// Telegram -> GitHub Actions relay with a GPT-style talker (no structured input needed).
//
// POST /tg/<TELEGRAM_SECRET_PATH>  (+ X-Telegram-Bot-Api-Secret-Token check)
//   free text (anything, reply or not) -> talker (Groq gpt-oss-20b) decides:
//     chat   -> direct reply, no pipeline run
//     fix    -> dispatch kind=fix-dryrun, preview posted by the workflow,
//               pending confirm stored in KV; YES -> kind=fix-apply
//   `done` (or go/process)                -> dispatch staged video/link + images
//   `cancel`                              -> clear staging (+ pending confirm), dispatch nothing
//   text containing a URL                 -> stage the link, wait for `done`
//   photo / image file                   -> stage it (caption = label + time), wait
//   video attachment <= 20 MB             -> stage it, wait for `done`
//   video attachment > 20 MB              -> reply asking for a Drive link
// GET /health -> 200
//
// Talk policy lives here at the edge (fast, ~1 cheap LLM call per message);
// ALL edit decisions stay in-repo (parse -> dry-run preview -> apply-preview).
// Staging lives in KV (1h TTL): pending_video:<chat> + pending:<chat>.
// Dialogue memory: conv:<chat> (last 6 turns, 1h TTL).
// Pending confirm: pending_confirm:<chat> {state_ref, instruction, ts} (30 min).
// Last delivered video is resolved on demand via the GitHub artifacts API
// (latest state-<sha8>), so free text never needs a reply-to message.
// Image captions carry label + placement, e.g. `pyramid at 0:20`,
// `diagram 0:45-0:50`, `chart @1m20s` — any timestamp formatting works,
// the planner maps times to words and falls back to best-fit.
//
// Secrets (Cloudflare secret_text): TELEGRAM_BOT_TOKEN, TELEGRAM_SECRET_PATH,
// TELEGRAM_SECRET_TOKEN, GH_TOKEN, ALLOWED_CHAT_ID, GROQ_API_KEY.
// Without GROQ_API_KEY the bot falls back to legacy behavior (reply-to fix
// dispatches immediately, anything else gets usage help).
// Bindings: PENDING_IMAGES (KV namespace "vedit-pending-images").

const GITHUB_API = "https://api.github.com";
const GROQ_URL = "https://api.groq.com/openai/v1/chat/completions";
const CHAT_MODEL = "openai/gpt-oss-20b";
const TG_MAX_VIDEO_BYTES = 20 * 1024 * 1024;
const DONE_WORDS = new Set(["done", "go", "process", "process it", "start", "confirm"]);
const CANCEL_WORDS = new Set(["cancel", "clear", "reset"]);
const CONV_TTL = 3600;
const CONFIRM_TTL = 1800;
const CONFIRM_STALE_MS = 30 * 60 * 1000;
const CONV_TURNS = 6;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return new Response("ok", { status: 200 });
    }
    const expectedPath = `/tg/${env.TELEGRAM_SECRET_PATH}`;
    if (request.method !== "POST" || url.pathname !== expectedPath) {
      return new Response("not found", { status: 404 });
    }
    const secret = request.headers.get("x-telegram-bot-api-secret-token") || "";
    if (!env.TELEGRAM_SECRET_TOKEN || secret !== env.TELEGRAM_SECRET_TOKEN) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad json", { status: 400 });
    }

    let result = "ok";
    try {
      await handleUpdate(update, env);
    } catch (err) {
      const chatId = update?.message?.chat?.id ?? update?.edited_message?.chat?.id;
      result = `relay error: ${String(err?.message || err).slice(0, 300)}`;
      if (chatId) {
        try {
          await sendText(env, chatId, result);
        } catch {
          // never turn a notify failure into a Telegram retry loop
        }
      }
    }
    // always 200: Telegram retries non-2xx, and we already handled the failure
    return new Response(result, { status: 200 });
  },
};

async function handleUpdate(update, env) {
  const msg = update.message || update.edited_message || update.channel_post;
  if (!msg) return;

  const chatId = String(msg.chat?.id ?? "");
  if (chatId !== String(env.ALLOWED_CHAT_ID)) {
    return; // public surface: ignore everyone but the owner
  }

  const text = (msg.text || msg.caption || "").trim();
  const hasMedia = Boolean(msg.video || msg.photo || msg.document);

  // 1) reply context: a state ref becomes a hint for the talker, not a dispatch.
  // Legacy path (no GROQ_API_KEY): reply-to + text dispatches kind=fix directly.
  // A reply to a bot message with pending confirm routes to the talker too
  // (previews carry a state ref, but the user often just answers "this one").
  const pending = await getPendingConfirm(env, chatId); // null-clears when stale
  let replyRef = null;
  const reply = msg.reply_to_message;
  if (reply) {
    const stateRef = String(reply.text || reply.caption || "").match(/\b[0-9a-f]{8}\b/);
    if (stateRef) {
      if (!text) {
        await sendText(
          env,
          chatId,
          'send the correction as text, e.g. "place image 1 at 0:20"',
        );
        return;
      }
      if (!env.GROQ_API_KEY) {
        await dispatch(env, {
          kind: "fix",
          chat_id: chatId,
          state_ref: stateRef[0],
          instruction: text,
          attachments: JSON.stringify(
            (await takePending(env, chatId)).map((im) => im.id),
          ),
        });
        await sendText(env, chatId, `queued fix ${stateRef[0]} — new cut coming`);
        return;
      }
      replyRef = stateRef[0];
    } else if (reply.from?.is_bot) {
      if (pending) {
        replyRef = pending.state_ref;
      } else {
        await sendText(
          env,
          chatId,
          "that message has no state ref — just tell me which video and what to change",
        );
        return;
      }
    }
  }

  // 2) staging commands (plain text only — never a reply/caption/media).
  // A pending confirm reroutes `done`-words to the talker (likely "confirm").
  const cmd = text.toLowerCase();
  if (!reply && !hasMedia) {
    if (CANCEL_WORDS.has(cmd)) {
      await clearPendingConfirm(env, chatId);
      await takePending(env, chatId);
      await clearVideo(env, chatId);
      await sendText(
        env,
        chatId,
        pending
          ? "scrapped — staging empty and the pending edit is cancelled."
          : "cleared — staging empty. Send a video when ready.",
      );
      return;
    }
    if (DONE_WORDS.has(cmd) && !pending) {
      const video = await getVideo(env, chatId);
      if (!video) {
        const staged = await getPending(env, chatId);
        await sendText(
          env,
          chatId,
          staged.length
            ? `no video staged yet — ${staged.length} image(s) waiting. Send the video or link first (or reply to a delivered video to add them to it).`
            : "nothing staged yet — send a video or link first, then images, then `done`.",
        );
        return;
      }
      const imgs = await takePending(env, chatId);
      await clearVideo(env, chatId);
      const inputs = {
        kind: "new",
        chat_id: chatId,
        prompt: buildPrompt(video.prompt, imgs),
        attachments: JSON.stringify(imgs.map((im) => im.id)),
      };
      if (video.kind === "url") inputs.url = video.ref;
      else inputs.video_file_id = video.ref;
      await dispatch(env, inputs);
      await sendText(
        env,
        chatId,
        imgs.length
          ? `queued — building v1 from your video + ${imgs.length} image(s)`
          : "queued — processing your video now",
      );
      return;
    }
  }

  // 3) URL in text -> stage the link, wait for `done`
  const urlMatch = text.match(/https?:\/\/\S+/);
  if (urlMatch && !msg.video) {
    const prompt = text.replace(urlMatch[0], "").trim();
    await setVideo(env, chatId, { kind: "url", ref: urlMatch[0], prompt });
    const staged = await getPending(env, chatId);
    await sendText(
      env,
      chatId,
      `link received — ${staged.length} image(s) already staged. ` +
        "Send images with captions (`name at 0:20`), then `done` and I'll build v1. Nothing runs until then.",
    );
    return;
  }

  // 4) photo / image file -> stage it (caption = label + time)
  const photoSizes = Array.isArray(msg.photo) ? msg.photo : [];
  const photo = photoSizes.length ? photoSizes[photoSizes.length - 1] : null;
  const imageDoc =
    msg.document && /^image\//.test(msg.document.mime_type || "")
      ? msg.document
      : null;
  const image = photo || imageDoc;
  if (image && !msg.video) {
    const note = (msg.caption || "").trim();
    const count = await pushPending(env, chatId, image.file_id, note);
    const stagedVideo = await getVideo(env, chatId);
    const capUrl = note.match(/https?:\/\/\S+/);
    if (capUrl && !stagedVideo) {
      await setVideo(env, chatId, {
        kind: "url",
        ref: capUrl[0],
        prompt: note.replace(capUrl[0], "").trim(),
      });
    }
    const p = parsePlacement(note, `image ${count}`);
    const echo =
      p.start == null
        ? ""
        : ` (${p.label} · ${p.end == null ? "from " + fmtTime(p.start) : fmtTime(p.start) + "–" + fmtTime(p.end)})`;
    await sendText(
      env,
      chatId,
      `saved image ${count}${echo} — ` +
        (stagedVideo || capUrl
          ? "send more, then `done` and I'll build v1."
          : "now send the video (or link), then `done`."),
    );
    return;
  }

  // 5) video attachment -> stage it (<= 20 MB), wait for `done`
  const media =
    msg.video ||
    (msg.document && /^video\//.test(msg.document.mime_type || "") ? msg.document : null);
  if (media) {
    const size = Number(media.file_size || 0);
    if (size > TG_MAX_VIDEO_BYTES) {
      await sendText(
        env,
        chatId,
        `video is ${(size / 1048576).toFixed(0)}MB — over the 20MB bot limit. Send a Google Drive link instead.`,
      );
      return;
    }
    await setVideo(env, chatId, {
      kind: "video",
      ref: media.file_id,
      prompt: text,
    });
    const staged = await getPending(env, chatId);
    await sendText(
      env,
      chatId,
      `video received — ${staged.length} image(s) already staged. ` +
        "Send images with captions (`name at 0:20`), then `done` and I'll build v1. Nothing runs until then.",
    );
    return;
  }

  // 6) free text -> GPT-style talker turn (no structured input needed).
  // Legacy path (no GROQ_API_KEY): usage help.
  if (!env.GROQ_API_KEY) {
    await sendText(
      env,
      chatId,
      "send a video or link (it waits here), then images with captions " +
        "(`pyramid at 0:20`), then `done` — v1 builds once, with everything placed. " +
        "Reply to a delivered video to correct it; `cancel` clears staging.",
    );
    return;
  }
  await chatTurn(env, chatId, { text, replyRef, pending });
}

// One conversational turn: talker decides chat / fix / yes / no / adjust.
// Edit decisions always go through dry-run preview + explicit YES.
async function chatTurn(env, chatId, { text, replyRef, pending }) {
  const turns = await getConv(env, chatId);
  const staged = await getPending(env, chatId);
  const video = await getVideo(env, chatId);
  let lastState = replyRef || pending?.state_ref || null;
  if (!lastState) lastState = await getLatestStateRef(env);
  const talk = await askTalker(env, {
    text,
    turns,
    staged: { images: staged.length, video: Boolean(video) },
    pending: pending ? { op: pending.op, instruction: pending.instruction } : null,
    lastState,
    replyRef,
  });
  await pushConv(env, chatId, text, talk.reply || "(no reply text)");
  // Never go silent: an empty model reply still gets a human ack.
  if (!talk.reply) {
    talk.reply =
      talk.action === "fix" || talk.action === "adjust"
        ? "On it — previewing that now, one sec."
        : talk.action === "yes"
          ? "Applying now."
          : "Got it.";
  }
  if (talk.reply) await sendText(env, chatId, talk.reply);

  if (talk.action === "fix" || talk.action === "adjust") {
    const ref = talk.state_ref || replyRef || pending?.state_ref || lastState;
    const instruction = talk.instruction || text;
    if (!ref) {
      await sendText(
        env,
        chatId,
        "which video should I change? Reply to a delivered video and tell me the edit.",
      );
      return;
    }
    await setPendingConfirm(env, chatId, {
      state_ref: ref,
      instruction,
      op: talk.action,
      ts: Date.now(),
    });
    await dispatch(env, {
      kind: "fix-dryrun",
      chat_id: chatId,
      state_ref: ref,
      instruction,
      attachments: JSON.stringify((await takePending(env, chatId)).map((im) => im.id)),
    });
    return;
  }
  if (talk.action === "deliver") {
    const ref = talk.state_ref || replyRef || lastState;
    if (!ref) {
      await sendText(
        env,
        chatId,
        "which video should I send? Reply to a delivered video.",
      );
      return;
    }
    await dispatch(env, {
      kind: "deliver",
      chat_id: chatId,
      state_ref: ref,
      instruction: "",
    });
    return;
  }
  if (talk.action === "yes") {
    if (!pending) {
      await sendText(
        env,
        chatId,
        "nothing waiting for approval — tell me an edit and I'll preview it first.",
      );
      return;
    }
    await clearPendingConfirm(env, chatId);
    await dispatch(env, {
      kind: "fix-apply",
      chat_id: chatId,
      state_ref: pending.state_ref,
      instruction: "",
    });
    return;
  }
  if (talk.action === "no") {
    if (pending) await clearPendingConfirm(env, chatId);
    return; // ack already sent as the reply
  }
  // chat / unknown: reply already sent, nothing to dispatch.
}

// Staged input images, buffered in KV between messages (1h TTL).
// Entries are {id, note}; plain-string entries from older versions are
// normalized on read. Missing binding -> behave as if nothing was staged.
function pendingKey(chatId) {
  return `pending:${chatId}`;
}

function videoKey(chatId) {
  return `pending_video:${chatId}`;
}

async function getPending(env, chatId) {
  try {
    if (!env.PENDING_IMAGES) return [];
    const raw = await env.PENDING_IMAGES.get(pendingKey(chatId));
    const list = JSON.parse(raw || "[]");
    if (!Array.isArray(list)) return [];
    return list
      .map((v) => (typeof v === "string" ? { id: v, note: "" } : v))
      .filter((v) => v && typeof v.id === "string");
  } catch {
    return [];
  }
}

async function pushPending(env, chatId, fileId, note) {
  const list = await getPending(env, chatId);
  list.push({ id: fileId, note: note || "" });
  if (env.PENDING_IMAGES) {
    await env.PENDING_IMAGES.put(pendingKey(chatId), JSON.stringify(list), {
      expirationTtl: 3600,
    });
  }
  return list.length;
}

async function takePending(env, chatId) {
  const list = await getPending(env, chatId);
  if (env.PENDING_IMAGES && list.length) {
    await env.PENDING_IMAGES.delete(pendingKey(chatId));
  }
  return list;
}

async function getVideo(env, chatId) {
  try {
    if (!env.PENDING_IMAGES) return null;
    const raw = await env.PENDING_IMAGES.get(videoKey(chatId));
    const v = JSON.parse(raw || "null");
    if (v && (v.kind === "url" || v.kind === "video") && typeof v.ref === "string") {
      return { kind: v.kind, ref: v.ref, prompt: String(v.prompt || "") };
    }
    return null;
  } catch {
    return null;
  }
}

async function setVideo(env, chatId, video) {
  if (env.PENDING_IMAGES) {
    await env.PENDING_IMAGES.put(videoKey(chatId), JSON.stringify(video), {
      expirationTtl: 3600,
    });
  }
}

async function clearVideo(env, chatId) {
  if (env.PENDING_IMAGES) {
    try {
      await env.PENDING_IMAGES.delete(videoKey(chatId));
    } catch {
      // staging is best-effort; the TTL cleans up regardless
    }
  }
}

// ---- GPT-style talk: dialogue memory, pending confirms, state lookup ----

function convKey(chatId) {
  return `conv:${chatId}`;
}

function confirmKey(chatId) {
  return `pending_confirm:${chatId}`;
}

async function getConv(env, chatId) {
  try {
    if (!env.PENDING_IMAGES) return [];
    const list = JSON.parse((await env.PENDING_IMAGES.get(convKey(chatId))) || "[]");
    return Array.isArray(list) ? list.filter((t) => t && t.r && t.t).slice(-CONV_TURNS) : [];
  } catch {
    return [];
  }
}

async function pushConv(env, chatId, userText, assistantText) {
  try {
    if (!env.PENDING_IMAGES) return;
    const turns = await getConv(env, chatId);
    turns.push({ r: "u", t: String(userText || "").slice(0, 500) });
    if (assistantText) turns.push({ r: "a", t: String(assistantText).slice(0, 500) });
    await env.PENDING_IMAGES.put(convKey(chatId), JSON.stringify(turns.slice(-CONV_TURNS)), {
      expirationTtl: CONV_TTL,
    });
  } catch {
    // memory is best-effort; never break the turn
  }
}

async function getPendingConfirm(env, chatId) {
  try {
    if (!env.PENDING_IMAGES) return null;
    const raw = await env.PENDING_IMAGES.get(confirmKey(chatId));
    if (!raw) return null;
    const p = JSON.parse(raw);
    if (!p || !p.state_ref || Date.now() - (p.ts || 0) > CONFIRM_STALE_MS) {
      await env.PENDING_IMAGES.delete(confirmKey(chatId));
      return null;
    }
    return p;
  } catch {
    return null;
  }
}

async function setPendingConfirm(env, chatId, pending) {
  try {
    if (!env.PENDING_IMAGES) return;
    await env.PENDING_IMAGES.put(confirmKey(chatId), JSON.stringify(pending), {
      expirationTtl: CONFIRM_TTL,
    });
  } catch {
    // best-effort; apply-preview still guards on plan hash
  }
}

async function clearPendingConfirm(env, chatId) {
  try {
    if (env.PENDING_IMAGES) await env.PENDING_IMAGES.delete(confirmKey(chatId));
  } catch {
    // best-effort
  }
}

// Newest delivered state, so free text never needs a reply-to message.
async function getLatestStateRef(env) {
  try {
    const resp = await fetch(
      `${GITHUB_API}/repos/${env.GH_OWNER}/${env.GH_REPO}/actions/artifacts?per_page=20`,
      {
        headers: {
          Authorization: `Bearer ${env.GH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "vedit-relay",
        },
      },
    );
    if (!resp.ok) return null;
    const data = await resp.json();
    const arts = Array.isArray(data?.artifacts) ? data.artifacts : [];
    const states = arts
      .filter((a) => /^state-[0-9a-f]{8}$/.test(a?.name || ""))
      .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
    const m = states.length ? states[0].name.match(/^state-([0-9a-f]{8})$/) : null;
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

function extractJson(text) {
  try {
    const start = String(text || "").indexOf("{");
    const end = String(text || "").lastIndexOf("}");
    if (start < 0 || end <= start) return null;
    return JSON.parse(String(text).slice(start, end + 1));
  } catch {
    return null;
  }
}

// Best-effort rescue of a truncated/prosed model reply: pull the known
// string fields straight out. Returns null unless action is a valid verb.
function salvageTalkerJson(text) {
  try {
    const grab = (key) => {
      const m = String(text || "").match(
        new RegExp('"' + key + '"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"'),
      );
      return m ? JSON.parse('"' + m[1] + '"') : "";
    };
    const action = grab("action");
    if (!/^(chat|fix|yes|no|adjust|deliver)$/.test(action)) return null;
    return {
      action,
      reply: String(grab("reply")).slice(0, 1000),
      instruction: String(grab("instruction")),
      state_ref: String(grab("state_ref")),
    };
  } catch {
    return null;
  }
}

// One small-model call per message (openai/gpt-oss-20b: fast, near-free).
// Edit decisions NEVER happen here — the talker only classifies + rephrases;
// the pipeline (dry-run preview -> explicit YES -> apply) decides.
async function askTalker(env, ctx) {
  const system =
    "You are Vedit, the owner's personal video editor, chatting on Telegram. " +
    "Warm, brief, human — like texting a real editor. Keep replies to 1-2 short " +
    "sentences unless explaining. Never mention models, prompts, word indices, or internals.\n\n" +
    "CONTEXT (JSON): " +
    JSON.stringify({
      staged: ctx.staged,
      pending: ctx.pending,
      last_state: ctx.lastState,
      reply_ref: ctx.replyRef,
    }) +    "\n\nYou CAN ask the pipeline to: cut/keep spans, place/move/remove/resize images, " +
    "recaption, add/remove zooms, replace icons. Times the user gives mean the " +
    "ORIGINAL uploaded video; the pipeline shows both clocks at confirm time — " +
    "never convert or second-guess times.\n" +
    "You CANNOT: music, effects, new footage, speed changes, caption styling. " +
    "Say so plainly and offer the closest alternative.\n\n" +
    "Decide ONE action, reply with raw JSON only:\n" +
    '{"action": "chat|fix|yes|no|adjust|deliver", ' +
    '"reply": "<message to send the owner NOW>", ' +
    '"instruction": "<self-contained edit text for fix/adjust>", ' +
    '"state_ref": "<8hex ref the edit targets, echo from context>"}\n' +
    "- chat: small talk, questions, help, status, anything needing no pipeline. Full answer in reply.\n" +
    "- fix: a concrete edit. reply = short ack like 'On it — previewing that cut, one sec.' " +
    "instruction = the complete edit. state_ref = reply_ref, pending ref, or last_state.\n" +
    "- yes: user confirms the pending edit (yes/yeah/do it/confirm/done...). reply = short ack.\n" +
    "- no: user rejects or cancels. reply = short ack.\n" +
    "- adjust: user tweaks the pending edit ('make it 0:40 instead'). instruction = the REVISED full edit. reply = short ack.\n" +
    "- Multiple edits in one message: instruction = the FIRST edit only; reply names it and promises the rest next.\n" +
    "- Pending exists: affirmations (yes/yess/yeah/yup/do it/go/confirm/ok/okay/done/this one/do this) are YES, always — never chat. Rejections are NO. Status questions ('is it done?', 'are you working on it') get reassurance with a time expectation, never 'which video?'.\n" +
    "- YES acks must set a time expectation ('Applying now — new cut lands here in a few minutes.') and never claim instant completion. A just-confirmed edit in conversation history answers later status questions the same way.\n" +
    "- Fix requested but no state ref exists anywhere: action chat, reply asks which video.\n" +
    "- Pending exists but the message is unrelated chit-chat: action chat, leave pending alone.\n" +
    "- deliver: resend the finished video ('send it again', 'I didn't get the video'). No preview needed — the file already exists. reply = short ack.";
  const messages = [
    { role: "system", content: system },
    ...ctx.turns.map((t) => ({
      role: t.r === "a" ? "assistant" : "user",
      content: t.t,
    })),
    { role: "user", content: ctx.text },
  ];
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const resp = await fetch(GROQ_URL, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GROQ_API_KEY}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          model: CHAT_MODEL,
          temperature: 0.3,
          max_tokens: 800,
          messages:
            attempt === 0
              ? messages
              : [
                  ...messages,
                  {
                    role: "user",
                    content:
                      "Your last reply was not valid JSON. Reply with raw JSON only.",
                  },
                ],
        }),
      });
      if (!resp.ok) throw new Error(`groq ${resp.status}`);
      const data = await resp.json();
      const content = data?.choices?.[0]?.message?.content || "";
      const parsed = extractJson(content) || salvageTalkerJson(content);
      if (parsed && typeof parsed.action === "string") {
        return {
          action: parsed.action,
          reply: String(parsed.reply || "").slice(0, 1000),
          instruction: String(parsed.instruction || ""),
          state_ref: String(parsed.state_ref || ""),
        };
      }
    } catch {
      // fall through to the quiet retry, then the garbled-reply fallback
    }
  }
  return {
    action: "chat",
    reply: "hmm, that came out garbled — say the edit again?",
  };
}

function fmtTime(sec) {
  const s = Math.max(0, Math.round(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// Free-form caption -> {start, end, label}. Accepts 0:20, 0:20-0:26,
// 45s, 1m20s, @-prefixes — anything else falls back to best-fit placement.
function parsePlacement(caption, fallbackLabel) {
  const text = String(caption || "");
  const spans = [];
  const push = (v, i, len) => {
    if (Number.isFinite(v) && v >= 0) spans.push({ v, i, len });
  };
  let m;
  const mmss = /(\d+):(\d{1,2})/g;
  while ((m = mmss.exec(text))) push(+m[1] * 60 + +m[2], m.index, m[0].length);
  const mins = /(\d+)\s*m(?:in(?:ute)?s?)?(?:\s*(\d+(?:\.\d+)?)\s*s)?/gi;
  const minSpans = [];
  while ((m = mins.exec(text))) {
    push(+m[1] * 60 + (m[2] != null ? +m[2] : 0), m.index, m[0].length);
    minSpans.push([m.index, m.index + m[0].length]);
  }
  const secs = /(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?\b/gi;
  while ((m = secs.exec(text))) {
    const s = m.index;
    const e = s + m[0].length;
    if (minSpans.some(([a, b]) => s >= a && e <= b)) continue; // inside 1m20s
    push(+m[1], s, m[0].length);
  }
  spans.sort((a, b) => a.i - b.i);
  let start = null;
  let end = null;
  if (spans.length) {
    start = spans[0].v;
    if (spans.length > 1) {
      const between = text.slice(spans[0].i + spans[0].len, spans[1].i);
      if (/^\s*(?:-|–|—|to)\s*$/i.test(between)) end = spans[1].v;
    }
  }
  let label = text;
  const cut = spans
    .map((t) => [t.i, t.i + t.len])
    .sort((a, b) => b[0] - a[0]);
  for (const [a, b] of cut) label = `${label.slice(0, a)} ${label.slice(b)}`;
  label = label.replace(/[-–—@,;:()[\]]/g, " ");
  label = label.replace(/\b(place|put|show|add|use|display|overlay)\b/gi, " ");
  label = label.replace(/\b(at|on|from|around|near|about|during|when|where)\b/gi, " ");
  label = label.replace(/\s+/g, " ").trim();
  if (!label) label = fallbackLabel;
  return { start, end, label };
}

function buildPrompt(base, imgs) {
  const lines = imgs.map((im, k) => {
    const p = parsePlacement(im.note || "", `image ${k + 1}`);
    const when =
      p.start == null
        ? "place where it fits best"
        : p.end == null
          ? `from ${fmtTime(p.start)}`
          : `${fmtTime(p.start)}–${fmtTime(p.end)}`;
    return `- image-${k + 1} (${p.label}): ${when}`;
  });
  if (!lines.length) return base;
  return (
    `${base}\n\nStaged images ` +
    `(attachments image-1..image-${imgs.length}, match by name stem):\n${lines.join("\n")}`
  );
}

function normalizedInputs(inputs) {
  const base = {
    kind: "",
    chat_id: "",
    url: "",
    video_file_id: "",
    attachments: "[]",
    prompt: "",
    state_ref: "",
    instruction: "",
  };
  const out = { ...base };
  for (const [key, value] of Object.entries(inputs)) {
    if (value !== undefined && value !== null) out[key] = String(value);
  }
  return out;
}

async function dispatch(env, inputs) {
  const resp = await fetch(
    `${GITHUB_API}/repos/${env.GH_OWNER}/${env.GH_REPO}/actions/workflows/edit.yml/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "vedit-relay", // GitHub API rejects requests without one
      },
      body: JSON.stringify({ ref: "main", inputs: normalizedInputs(inputs) }),
    },
  );
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    throw new Error(`github dispatch ${resp.status}: ${body.slice(0, 200)}`);
  }
}

async function sendText(env, chatId, text) {
  const resp = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text: text.slice(0, 4000) }),
  });
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    throw new Error(`telegram sendMessage ${resp.status}: ${body.slice(0, 200)}`);
  }
}
