// Telegram -> GitHub Actions relay with slash commands first, talker second.
//
// POST /tg/<TELEGRAM_SECRET_PATH>  (+ X-Telegram-Bot-Api-Secret-Token check)
//
// Slash commands (deterministic, never touch the LLM):
//   /new [link]                            stage the video/link to build from
//   /addimage                              photo caption hint (label + time)
//   /build (/done /go)                     dispatch staged video/link + images
//   /list /status /cancel /help            staging + run polling + wipe + menu
//   /change <anything>                     dry-run preview, no talker classification
//   /cut 0:30 0:45 · /keep 0:10 1:00       cut_range / keep_range previews
//   /place image 3 at 0:20 · /move · /remove · /size image 3 60%
//   /recaption 0:45 <new text>
//   /preset clean_voice|punchy|flat        audio recipe (denoise stays on)
//   /editimage [N]                         image menu: aspect / crop / size
//   /redo <sha8> · /apply · /reject · /deliver
//
// Inline buttons: every dry-run preview carries Apply/Reject buttons
// (callback_data `apply:<sha8>` / `reject:<sha8>`); /editimage / /preset menus
// answer callbacks (`menu:<ref>:<vid>:<key>:<value>` / `preset:<name>`) and
// dispatch previews for the same deterministic pipeline ops.
//
// Free text (anything, reply or not) -> talker (Groq gpt-oss-20b) decides:
//     chat   -> direct reply, no pipeline run
//     fix    -> dispatch kind=fix-dryrun, preview posted by the workflow,
//               pending confirm stored in KV; YES -> kind=fix-apply
//   `done` (or go/process)                -> dispatch staged video/link + images
//   `cancel`                              -> clear staging (+ pending confirm), dispatch nothing
//   text containing a URL (nothing staged)-> stage the link, wait for `done`
//   photo / image file                    -> stage it (caption = label + time), wait
//   video attachment <= 20 MB             -> stage it, wait for `done`
//   video attachment > 20 MB              -> reply asking for a Drive link
//
// Staging images are consumed ONLY by /build: previews and fix runs read them
// without wiping the list, so a misrouted message can never eat your staging.
//
// Talk policy lives here at the edge (fast, ~1 cheap LLM call per message);
// ALL edit decisions stay in-repo (parse -> dry-run preview -> apply-preview).
// Staging lives in KV (1h TTL): pending_video:<chat> + pending:<chat>.
// Dialogue memory: conv:<chat> (last 6 turns, 1h TTL).
// Pending confirm: pending_confirm:<chat> {state_ref, instruction, ts} (30 min).
// Edit menu state: edit_menu:<chat> {state_ref, vid, ts} (10 min).
// Last dispatch: last_run:<chat> {ts, kind, ref, instruction} (6h) — /status.
// Last delivered video is resolved on demand via the GitHub artifacts API
// (latest state-<sha8>), so free text never needs a reply-to message.
// Image captions carry label + placement, e.g. `pyramid at 0:20`,
// `diagram 0:45-0:50`, `chart @1m20s` — any timestamp formatting works,
// the planner maps times to words and falls back to best-fit.
//
// Secrets (Cloudflare secret_text): TELEGRAM_BOT_TOKEN, TELEGRAM_SECRET_PATH,
// TELEGRAM_SECRET_TOKEN, GH_TOKEN, ALLOWED_CHAT_ID, GROQ_API_KEY.
// Without GROQ_API_KEY the bot falls back to legacy behavior (reply-to fix
// dispatches immediately, anything else gets usage help); slash commands keep
// working either way.
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
const MENU_TTL = 600; // edit_menu / last_run-ish short state
const CMD_REG_TTL = 604800; // refresh the Telegram command menu weekly

// Telegram bot command menu (set via setMyCommands once per week; typed
// shortcuts below). Keep descriptions short — they show in the UI.
const BOT_COMMANDS = [
  { command: "new", description: "start a new edit — send a link or video next" },
  { command: "addimage", description: "add images (send photo, caption = label + time)" },
  { command: "build", description: "join staged video + images, build the cut" },
  { command: "status", description: "live status of the latest run" },
  { command: "list", description: "show what's staged right now" },
  { command: "cancel", description: "clear staging + pending previews" },
  { command: "change", description: "any edit, previewed first: /change cut the intro" },
  { command: "cut", description: "remove a span: /cut 0:30 0:45" },
  { command: "keep", description: "keep only a span: /keep 0:10 1:00" },
  { command: "place", description: "place an image: /place image 3 at 0:20" },
  { command: "move", description: "move an image: /move image 3 to 0:40" },
  { command: "remove", description: "remove an image: /remove image 3" },
  { command: "size", description: "resize an image: /size image 3 60%" },
  { command: "recaption", description: "redo captions: /recaption 0:45 better text" },
  { command: "editimage", description: "image menu: fill/blur/box + crop + size" },
  { command: "preset", description: "audio recipe: /preset clean_voice" },
  { command: "apply", description: "confirm the pending preview" },
  { command: "reject", description: "discard the pending preview" },
  { command: "deliver", description: "resend the finished video" },
  { command: "redo", description: "edit a different version: /redo a1b2c3d4" },
  { command: "help", description: "the full cheat-sheet" },
];

const HELP_TEXT =
  "*How to use the bot*\n" +
  "1. `/new` and send the video (or drive link) — or just send it right away\n" +
  "2. images: send photos, captions like `pyramid at 0:20`\n" +
  "3. `/build` → wait → you get the cut + a preview frame\n" +
  "Edits (every edit is previewed first, then YES/NO buttons):\n" +
  "`/change cut the intro` · `/cut 0:30 0:45` · `/keep 0:10 1:00`\n" +
  "`/place image 3 at 0:20` · `/move image 3 to 0:40` · `/remove image 3`\n" +
  "`/size image 3 60%` · `/recaption 0:45 better text`\n" +
  "`/editimage 3` → buttons: aspect (fill/blur/box) · crop (top/center/bottom) · size\n" +
  "`/preset clean_voice|punchy|flat` → audio recipe (noise-cancelling always on)\n" +
  "`/status` live progress · `/deliver` resend · `/redo a1b2c3d4` switch version\n" +
  "Typos welcome too — plain text still works, `cancel` always wipes staging.";

// Instruction phrasing per command — matches what the pipeline (LLM parse +
// deterministic salve) is happiest reading.
function fixInstruction(cmd, args) {
  const a = args.replace(/\s+/g, " ").trim();
  if (cmd === "change") return a;
  if (cmd === "cut") return a.startsWith("cut") ? a : `cut from ${a}`;
  if (cmd === "keep" || cmd === "clip") return a.startsWith("keep") ? a : `keep only ${a}`;
  if (cmd === "place") return a.startsWith("place") ? a : `place ${a}`;
  if (cmd === "move") return a.startsWith("move") ? a : `move the visual ${a}`;
  if (cmd === "remove") return a.startsWith("remove") ? a : `remove ${a}`;
  if (cmd === "size") return a.startsWith("resize") ? a : `resize ${a}`;
  if (cmd === "recaption") return a.startsWith("recaption") ? a : `recaption ${a}`;
  if (cmd === "preset") return a.startsWith("preset") ? a : `preset ${a}`;
  return a;
}

const CMD_USAGE = new Map([
  ["cut", "usage: `/cut 0:30 0:45` (or /cut where I say um)"],
  ["keep", "usage: `/keep 0:10 1:00` — cut everything else"],
  ["clip", "usage: `/clip 0:10 1:00` — same as /keep"],
  ["place", "usage: `/place image 3 at 0:20`"],
  ["move", "usage: `/move image 3 to 0:40`"],
  ["remove", "usage: `/remove image 3` or `/remove the image at 0:54`"],
  ["size", "usage: `/size image 3 60%` or `/size image 3 bigger`"],
  ["recaption", "usage: `/recaption 0:45 your corrected line`"],
  ["preset", "usage: `/preset clean_voice` (also punchy / flat)"],
  ["change", "usage: `/change cut the bit where I ramble about pricing`"],
  ["redo", "usage: `/redo a1b2c3d4` (any earlier state ref)"],
  ["new", 'usage: `/new https://drive...` — or "/new" then send the video'],
]);

// Audio recipes the pipeline accepts (`audio.presets` in config.yaml).
const KNOWN_PRESETS = ["clean_voice", "punchy", "flat"];

// /editimage menu presses → pipeline DIRECTIVE strings, parsed deterministically
// by vedit.stage.fix.parse_directive (no LLM) — see confirmation-contract.md.
function menuInstruction(key, value, vid) {
  if (key === "mode") return `set_visual_mode ${vid} ${value}`;
  if (key === "crop") return `set_visual_mode ${vid} cover crop ${value}`;
  if (key === "zoom") {
    if (value === "bigger") return `resize ${vid} bigger`;
    if (value === "full") return `resize ${vid} full`;
    if (value.startsWith("m")) return `resize ${vid} to ${value.slice(1)}%`;
    return `resize ${vid} to ${value}`;
  }
  return value;
}

// Rule-based affirmations ONLY fire when a preview is pending (so "yes" in
// small talk never applies an edit silently). Rejections likewise.
const STRICT_YES_RE =
  /^(y+e+s+h?|yes sir|yeah+|yup|ya?s+|sure|ok(ay)?|fine|go( ahead)?|do it|confirm(ed)?|apply( it)?|ship it|lgtm|this one|do this|perfect|great|good|please( do)?)\s*[.!]*$/i;
const STRICT_NO_RE =
  /^(no+p?|nope|nah|don'?t|dont do( it)?|reject( it)?|scrap|cancel( it)?|nevermind|never mind|stop|abort)\s*[.!]*$/i;
const REDELIVER_RE =
  /^(resend|redeliver|send (it|the video) again|send (me )?the video|send (me )?the file|i (don'?t|did'?t) (get|receive))\b/i;

const CALLBACK_LIMIT = 64; // Telegram callback_data max bytes

// Commands that turn into a previewed edit (dry-run dispatch).
const FIX_CMDS = new Set([
  "change",
  "cut",
  "keep",
  "clip",
  "place",
  "move",
  "remove",
  "size",
  "recaption",
  "preset",
]);

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
  // Inline keyboard presses: apply / reject previews and /editimage menus are
  // fully deterministic — they never touch the talker.
  if (update.callback_query) {
    await handleCallback(update.callback_query, env);
    return;
  }
  const msg = update.message || update.edited_message || update.channel_post;
  if (!msg) return;

  // Telegram retries an update when the reply is slow; never process twice.
  if (update.update_id != null && (await markUpdateSeen(env, update.update_id))) {
    return;
  }

  const chatId = String(msg.chat?.id ?? "");
  if (chatId !== String(env.ALLOWED_CHAT_ID)) {
    return; // public surface: ignore everyone but the owner
  }

  const text = (msg.text || msg.caption || "").trim();
  if (text) await ensureBotCommands(env);

  // Typing a slash command is a hard contract: route it deterministically,
  // before reply logic, staging or the talker get any say.
  if (msg.text && text.startsWith("/")) {
    await routeCommand(env, msg, chatId);
    return;
  }

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
          'send the correction as text — e.g. "place image 1 at 0:20", or use /editimage',
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
            (await peekPending(env, chatId)).map((im) => im.id),
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

  // 2) staging: `done`/`go` words build when nothing else is pending.
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
      await buildCommand(env, chatId, "done");
      return;
    }
  }

  // 3) URL in text -> stage the link, wait for `done`. Never silently
  // overwrite an already-staged video: a "check this link" aside would lose it.
  const urlMatch = text.match(/https?:\/\/\S+/);
  if (urlMatch && !msg.video) {
    if (await getVideo(env, chatId)) {
      await sendText(
        env,
        chatId,
        "a video is already staged — send 'cancel' first, or use '/new <link>' to replace it.",
      );
      return;
    }
    const prompt = text.replace(urlMatch[0], "").trim();
    await setVideo(env, chatId, { kind: "url", ref: urlMatch[0], prompt });
    const staged = await getPending(env, chatId);
    await sendText(
      env,
      chatId,
      `link received — ${staged.length} image(s) already staged. ` +
        "Send images with captions (`name at 0:20`), then 'done' and I'll build v1. Nothing runs until then.",
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
    // A caption may carry the command word ("/addimage pyramid at 0:20") —
    // the label is what follows, so strip one leading slash token.
    let note = (msg.caption || "").trim();
    if (note.startsWith("/")) {
      note = note.replace(/^\/[a-z_]+(?:@\S+)?/i, "").trim();
    }
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
          ? "send more, then 'done' and I'll build v1."
          : "now send the video (or link), then 'done'."),
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
        "Send images with captions (`name at 0:20`), then 'done' and I'll build v1. Nothing runs until then.",
    );
    return;
  }

  // 6) anything else that carries no text (voice notes, stickers, audio files)
  // can't be scheduled — say what IS supported instead of routing an empty
  // message into the talker.
  if (!text) {
    const kind =
      msg.voice ? "voice messages"
      : msg.audio ? "audio files"
      : msg.sticker ? "stickers"
      : msg.video_note ? "video notes"
      : null;
    if (kind) {
      await sendText(
        env,
        chatId,
        `${kind} aren't supported — type the edit as text, or use the slash commands (/help).`,
      );
      return;
    }
  }

  // 7) free text -> GPT-style talker turn (no structured input needed).
  // Legacy path (no GROQ_API_KEY): usage help.
  if (!env.GROQ_API_KEY) {
    await sendText(env, chatId, HELP_TEXT);
    return;
  }
  await chatTurn(env, chatId, { text, replyRef, pending });
}

// One conversational turn: talker decides chat / fix / yes / no / adjust.
// Edit decisions always go through dry-run preview + explicit YES.
// Hard cases (a pending YES/NO with an unambiguous answer) are decided by
// rules BEFORE the talker — the model never gets to misread "yess" again.
async function chatTurn(env, chatId, { text, replyRef, pending }) {
  if (pending && STRICT_YES_RE.test(text)) {
    await clearPendingConfirm(env, chatId);
    await sendText(
      env,
      chatId,
      "Applying now — new cut lands here in a few minutes.",
    );
    await dispatch(env, {
      kind: "fix-apply",
      chat_id: chatId,
      state_ref: pending.state_ref,
      instruction: "",
    });
    return;
  }
  if (pending && STRICT_NO_RE.test(text)) {
    await clearPendingConfirm(env, chatId);
    await sendText(env, chatId, "discarded — nothing was applied.");
    return;
  }
  if (REDELIVER_RE.test(text)) {
    const ref = await getLatestStateRef(env);
    await deliverRef(env, chatId, ref);
    return;
  }
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
          ? "Applying now — new cut lands here in a few minutes."
          : "Got it.";
  }
  if (talk.reply) await sendText(env, chatId, talk.reply);

  if (talk.action === "fix" || talk.action === "adjust") {
    const ref = talk.state_ref || replyRef || pending?.state_ref || lastState;
    const instruction = talk.instruction || text;
    await previewFix(env, chatId, instruction, ref);
    return;
  }
  if (talk.action === "deliver") {
    const ref = talk.state_ref || replyRef || lastState;
    await deliverRef(env, chatId, ref);
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

// ---- slash commands: deterministic routing, zero talker involvement ----

function parseCommand(text) {
  const m = String(text).match(/^\/([a-z_]+)(?:@[\w]+)?(\s+[\s\S]*|\s*)?$/i);
  if (!m) return null;
  return { cmd: m[1].toLowerCase(), args: (m[2] || "").trim().replace(/^\/[a-z_]+(.*)$/i, "$1").trim() };
}

async function routeCommand(env, msg, chatId) {
  const parsed = parseCommand((msg.text || "").trim());
  if (!parsed) return; // not a well-formed command — fall through as text
  const { cmd, args } = parsed;
  const pending = await getPendingConfirm(env, chatId);

  switch (cmd) {
    case "help":
    case "start":
      await sendText(env, chatId, HELP_TEXT);
      return;
    case "list": {
      const imgs = await getPending(env, chatId);
      const video = await getVideo(env, chatId);
      const lines = imgs.map((im, i) => {
        const p = parsePlacement(im.note || "", `image ${i + 1}`);
        const t =
          p.start == null
            ? ""
            : ` · ${p.end == null ? "from " + fmtTime(p.start) : fmtTime(p.start) + "–" + fmtTime(p.end)}`;
        return `• ${p.label}${t}`;
      });
      await sendText(
        env,
        chatId,
        (video ? `video: staged (${video.kind === "url" ? "link" : "file"})\n` : "video: none staged\n") +
          (lines.length ? lines.join("\n") : "no images staged — send photos or /addimage"),
      );
      return;
    }
    case "status":
      await runStatus(env, chatId);
      return;
    case "cancel":
      await clearPendingConfirm(env, chatId);
      await takePending(env, chatId);
      await clearVideo(env, chatId);
      await sendText(
        env,
        chatId,
        (pending ? "edit scrapped and " : "") + "staging cleared. Send a video when ready.",
      );
      return;
    case "new": {
      const url = args.match(/https?:\/\/\S+/);
      if (url) {
        await setVideo(env, chatId, {
          kind: "url",
          ref: url[0],
          prompt: args.replace(url[0], "").trim(),
        });
        await sendText(
          env,
          chatId,
          "link staged — send images with captions if you want them, then 'done' (or /build).",
        );
        return;
      }
      if (args) {
        await sendText(env, chatId, CMD_USAGE.get("new"));
        return;
      }
      await sendText(env, chatId, "send the video (<= 20MB) or a Drive link now — I'll stage it.");
      return;
    }
    case "addimage":
      await sendText(
        env,
        chatId,
        "send the photo(s) now — captions carry the label + time, e.g. `pyramid at 0:20`.",
      );
      return;
    case "build":
    case "done":
    case "go":
      await buildCommand(env, chatId, cmd);
      return;
    case "apply": {
      if (!pending) {
        await sendText(env, chatId, "nothing waiting for approval — nothing applied.");
        return;
      }
      await clearPendingConfirm(env, chatId);
      await sendText(env, chatId, "Applying now — new cut lands here in a few minutes.");
      await dispatch(env, {
        kind: "fix-apply",
        chat_id: chatId,
        state_ref: pending.state_ref,
        instruction: "",
      });
      return;
    }
    case "reject": {
      if (pending) await clearPendingConfirm(env, chatId);
      await sendText(env, chatId, "discarded — nothing was applied.");
      return;
    }
    case "deliver":
      await deliverRef(env, chatId, args.match(/\b[0-9a-f]{8}\b/)?.[0] || null);
      return;
    case "redo": {
      const ref = args.match(/\b[0-9a-f]{8}\b/)?.[0];
      if (!ref) {
        await sendText(env, chatId, CMD_USAGE.get("redo"));
        return;
      }
      await setPendingConfirm(env, chatId, {
        state_ref: ref,
        instruction: "",
        op: "fix",
        ts: Date.now(),
      });
      await sendText(env, chatId, `target set to ${ref} — what should I change? (or /editimage)`);
      return;
    }
    case "editimage": {
      const n = args.match(/\d+/);
      const ref =
        String(msg.reply_to_message?.text || "")
          .match(/\b[0-9a-f]{8}\b/)?.[0] || null;
      await editImageMenu(env, chatId, ref, n ? `image-${n[0]}` : null);
      return;
    }
    case "preset": {
      const name = args.trim().toLowerCase();
      if (!name) {
        if (KNOWN_PRESETS.length) {
          const rows = KNOWN_PRESETS.map((p) => [
            { text: p, callback_data: `preset:${p}` },
          ]);
          await sendText(env, chatId, "pick the audio recipe:", { inline_keyboard: rows });
          return;
        }
        await sendText(env, chatId, CMD_USAGE.get("preset"));
        return;
      }
      await previewFix(env, chatId, fixInstruction("preset", name), null);
      return;
    }
    default: {
      const instruction = fixInstruction(cmd, args);
      const valid = FIX_CMDS.has(cmd);
      if (!valid) {
        await sendText(env, chatId, `unknown command '/${cmd}' — here's the menu:` + "\n" + HELP_TEXT);
        return;
      }
      if (!args) {
        await sendText(env, chatId, CMD_USAGE.get(cmd) || "what should I do?");
        return;
      }
      await previewFix(
        env,
        chatId,
        instruction,
        String(msg.reply_to_message?.text || "").match(/\b[0-9a-f]{8}\b/)?.[0] ||
          pending?.state_ref ||
          null, // previewFix falls back to the newest state itself
      );
    }
  }
}

// Build from staged video + images: the only path that consumes staging.
async function buildCommand(env, chatId, source) {
  const video = await getVideo(env, chatId);
  if (!video) {
    const staged = await getPending(env, chatId);
    const known =
      source === "build" ? "/build" : `'${source}'`;
    await sendText(
      env,
      chatId,
      staged.length
        ? `no video staged yet — ${staged.length} image(s) waiting. Send the video or a link first.`
        : `nothing staged — send a video or link (or '/new <link>'), then ${known}.`,
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
      ? `queued — building v1 from your video + ${imgs.length} image(s). /status for progress.`
      : "queued — processing your video now. /status for progress.",
  );
}

// Read-only look at staged images (previews must never eat staging).
async function peekPending(env, chatId) {
  return getPending(env, chatId);
}

// Resolve a state ref (explicit > reply > pending > newest), store the pending
// confirm and dispatch the dry-run. Staging images stay in KV.
async function previewFix(env, chatId, instruction, refHint, { silent } = {}) {
  const pending = await getPendingConfirm(env, chatId);
  let ref = refHint || null;
  if (!ref) ref = (await getLatestStateRef(env)) || null;
  if (!ref) {
    await sendText(
      env,
      chatId,
      "no video to edit yet — build one first (send a video, then 'done').",
    );
    return;
  }
  const samePending =
    pending &&
    pending.state_ref === ref &&
    pending.instruction === instruction;
  if (!samePending) {
    await setPendingConfirm(env, chatId, {
      state_ref: ref,
      instruction,
      op: "fix",
      ts: Date.now(),
    });
  }
  await dispatch(env, {
    kind: "fix-dryrun",
    chat_id: chatId,
    state_ref: ref,
    instruction,
    attachments: JSON.stringify((await peekPending(env, chatId)).map((im) => im.id)),
  });
  if (!silent) {
    await sendText(
      env,
      chatId,
      `previewing: “${instruction.slice(0, 120)}” — frame + summary arrive in about a minute, then YES/NO buttons.`,
    );
  }
}

async function deliverRef(env, chatId, refHint) {
  let ref = refHint;
  if (!ref) ref = (await getLatestStateRef(env)) || null;
  if (!ref) {
    await sendText(env, chatId, "no delivered video yet to resend.");
    return;
  }
  await dispatch(env, {
    kind: "deliver",
    chat_id: chatId,
    state_ref: ref,
    instruction: "",
  });
  await sendText(env, chatId, `resending ${ref} — check your media in a jiffy.`);
}

// ---- inline keyboards: confirm + /editimage + /preset menus ----

async function handleCallback(cb, env) {
  const chatId = String(cb.message?.chat?.id ?? cb.from?.id ?? "");
  if (chatId !== String(env.ALLOWED_CHAT_ID)) {
    await answerCallback(env, cb.id, "not for this chat");
    return;
  }
  const data = String(cb.data || "").slice(0, CALLBACK_LIMIT);
  const parts = data.split(":");
  const [tag] = parts;

  if (tag === "apply" && parts.length === 2) {
    const ref = parts[1];
    const pending = await getPendingConfirm(env, chatId);
    if (!pending || pending.state_ref !== ref) {
      await answerCallback(env, cb.id, "that preview expired — make the edit again");
      return;
    }
    await clearPendingConfirm(env, chatId);
    await answerCallback(env, cb.id, "Applying — new cut in a few minutes");
    await sendText(env, chatId, "Applying now — new cut lands here in a few minutes.");
    await dispatch(env, {
      kind: "fix-apply",
      chat_id: chatId,
      state_ref: ref,
      instruction: "",
    });
    return;
  }
  if (tag === "reject" && parts.length === 2) {
    const ref = parts[1];
    const pending = await getPendingConfirm(env, chatId);
    if (pending && pending.state_ref === ref) await clearPendingConfirm(env, chatId);
    await answerCallback(env, cb.id, "discarded");
    return;
  }
  if (tag === "preset" && parts.length === 2 && KNOWN_PRESETS.includes(parts[1])) {
    await answerCallback(env, cb.id, "Previewing the audio recipe…");
    await previewFix(env, chatId, `preset ${parts[1]}`, null, { silent: true });
    return;
  }
  if (tag === "edil" && parts.length === 3) {
    // image picked -> show the aspect/crop/size grid for that image
    await setEditMenu(env, chatId, { state_ref: parts[1], vid: parts[2], ts: Date.now() });
    await answerCallback(env, cb.id, "pick an aspect");
    await sendMenuButtons(env, chatId, parts[2]);
    return;
  }
  if (tag === "menu" && parts.length === 5) {
    const [, ref, vid, key, value] = parts;
    await answerCallback(env, cb.id, "Previewing…");
    await previewFix(env, chatId, menuInstruction(key, value, vid), ref, { silent: true });
    return;
  }
  await answerCallback(env, cb.id, "unknown action");
}

const MODE_GRID = [
  [
    { text: "▶ Fill reel", callback_data: "mode:cover" },
    { text: "🌤 Fit + blur", callback_data: "mode:adaptive" },
    { text: "▢ Floating box", callback_data: "mode:overlay" },
  ],
];

const CROP_GRID = [
  [
    { text: "⬆ top", callback_data: "crop:top" },
    { text: "⏺ center", callback_data: "crop:center" },
    { text: "⬇ bottom", callback_data: "crop:bottom" },
  ],
];

const SIZE_GRID = [
  [
    { text: "+ Tighter", callback_data: "zoom:bigger" },
    { text: "− Wider", callback_data: "zoom:m60" },
    { text: "▣ Full", callback_data: "zoom:full" },
  ],
  [
    { text: "50%", callback_data: "zoom:m50" },
    { text: "60%", callback_data: "zoom:m60" },
    { text: "80%", callback_data: "zoom:m80" },
  ],
];

function menuButtons(stateRef, vid) {
  const fill = (grid) =>
    grid.map((row) =>
      row.map((b) => ({
        text: b.text,
        callback_data: `menu:${stateRef}:${vid}:${b.callback_data}`,
      })),
    );
  return {
    inline_keyboard: [
      ...fill(MODE_GRID),
      ...fill(CROP_GRID),
      ...fill(SIZE_GRID),
    ],
  };
}

async function sendMenuButtons(env, chatId, vid) {
  const ref = (await getLatestStateRef(env)) || null;
  if (!ref) {
    await sendText(env, chatId, "no delivered video yet — /editimage works after the first cut.");
    return;
  }
  await sendText(
    env,
    chatId,
    `image ${vid} — aspect, then crop (fill modes), then size:`,
    menuButtons(ref, vid),
  );
}

async function editImageMenu(env, chatId, ref, vid) {
  const stateRef = ref || (await getLatestStateRef(env)) || null;
  if (!stateRef) {
    await sendText(env, chatId, "no delivered video yet — /editimage works after the first cut.");
    return;
  }
  if (vid) {
    await setEditMenu(env, chatId, { state_ref: stateRef, vid, ts: Date.now() });
    await sendText(env, chatId, `image ${vid} — aspect, crop, size:`, menuButtons(stateRef, vid));
    return;
  }
  const rows = [];
  for (let i = 1; i <= 9; i += 3) {
    rows.push(
      [i, i + 1, i + 2].map((n) => ({
        text: `image ${n}`,
        callback_data: `edil:${stateRef}:image-${n}`,
      })),
    );
  }
  await sendText(env, chatId, "which image?", { inline_keyboard: rows });
}

async function setEditMenu(env, chatId, menu) {
  try {
    if (env.PENDING_IMAGES) {
      await env.PENDING_IMAGES.put(`edit_menu:${chatId}`, JSON.stringify(menu), {
        expirationTtl: MENU_TTL,
      });
    }
  } catch {
    // best-effort: menus are shortcuts, the typed commands always work
  }
}

async function getEditMenu(env, chatId) {
  try {
    if (!env.PENDING_IMAGES) return null;
    const raw = await env.PENDING_IMAGES.get(`edit_menu:${chatId}`);
    const m = raw ? JSON.parse(raw) : null;
    return m && m.state_ref && m.vid ? m : null;
  } catch {
    return null;
  }
}

// ---- /status: is the workflow actually doing anything? ----
// Latest dispatch is recorded per chat (last_run:<chat>); the newest matching
// GitHub Actions run for edit.yml answers queued / running / verdict.

async function runStatus(env, chatId) {
  try {
    let last = null;
    try {
      if (env.PENDING_IMAGES) {
        const raw = await env.PENDING_IMAGES.get(`last_run:${chatId}`);
        last = raw ? JSON.parse(raw) : null;
      }
    } catch {
      last = null;
    }
    if (!last) {
      await sendText(env, chatId, "no runs yet — send a video or a link, then 'done'.");
      return;
    }
    const resp = await fetch(
      `${GITHUB_API}/repos/${env.GH_OWNER}/${env.GH_REPO}/actions/workflows/edit.yml/runs?per_page=6`,
      {
        headers: {
          Authorization: `Bearer ${env.GH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "vedit-relay",
        },
      },
    );
    if (!resp.ok) throw new Error(`github runs ${resp.status}`);
    const data = await resp.json();
    const runs = Array.isArray(data?.workflow_runs) ? data.workflow_runs : [];
    const dispatchMs = Number(last.ts || 0);
    const run = runs.find(
      (r) => new Date(r.created_at || 0).getTime() >= dispatchMs - 20000,
    );
    const mins = Math.max(1, Math.round((Date.now() - dispatchMs) / 60000));
    const kind =
      last.kind === "new" ? "build"
      : last.kind === "fix-dryrun" ? "preview"
      : last.kind === "fix-apply" ? "apply"
      : last.kind;
    if (!run) {
      await sendText(
        env,
        chatId,
        `dispatched ${mins} min ago — still waiting for a runner (GitHub queueing).`,
      );
      return;
    }
    const age = Math.round((Date.now() - new Date(run.created_at).getTime()) / 60000);
    if (run.status !== "completed") {
      await sendText(
        env,
        chatId,
        `${kind} is running — started ${age} min ago (GitHub status: ${run.status}).`,
      );
      return;
    }
    const ok = run.conclusion === "success";
    await sendText(
      env,
      chatId,
      `${kind} ${ok ? "finished ✓" : `stopped (${run.conclusion})`} ${age} min ago` +
        `${last.ref ? ` — state ${last.ref}` : ""}. ` +
        (ok
          ? "the result should be in the chat above."
          : "no video was produced — you can retry the same command."),
    );
  } catch (err) {
    await sendText(
      env,
      chatId,
      `status check failed: ${String(err?.message || err).slice(0, 200)}`,
    );
  }
}

// ---- register the Telegram command menu once per week ----

async function ensureBotCommands(env) {
  try {
    if (!env.PENDING_IMAGES) return;
    if (await env.PENDING_IMAGES.get("botcmd:v2")) return;
    const resp = await fetch(
      `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/setMyCommands`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ commands: BOT_COMMANDS }),
      },
    );
    if (!resp.ok) return; // retry on a later message; errors stay silent
    await env.PENDING_IMAGES.put("botcmd:v2", "1", { expirationTtl: CMD_REG_TTL });
  } catch {
    // menu registration is conveniences-only
  }
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

// Already-processed Telegram update ids (retries after slow Groq calls).
// Returns true when this update was seen before.
async function markUpdateSeen(env, updateId) {
  try {
    if (!env.PENDING_IMAGES) return false;
    const key = `seen:${updateId}`;
    if (await env.PENDING_IMAGES.get(key)) return true;
    await env.PENDING_IMAGES.put(key, "1", { expirationTtl: 600 });
    return false;
  } catch {
    return false;
  }
}

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
    "The pipeline already transcribed the FULL video — every spoken word with " +
    "timestamps lives in the plan. NEVER ask the user to send or provide the " +
    "transcript. To place something 'where I talk about X': proceed to preview " +
    "(the planner maps phrases to words); if nothing matches, ask for a TIME, " +
    "never the transcript.\n" +
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
  // /status anchors: remember what we queued and when.
  try {
    if (env.PENDING_IMAGES && inputs.chat_id) {
      await env.PENDING_IMAGES.put(
        `last_run:${inputs.chat_id}`,
        JSON.stringify({
          ts: Date.now(),
          kind: String(inputs.kind || ""),
          ref: String(inputs.state_ref || ""),
          instruction: String(inputs.instruction || inputs.prompt || "").slice(0, 80),
        }),
        { expirationTtl: 21600 },
      );
    }
  } catch {
    // best-effort
  }
}

async function sendText(env, chatId, text, markup) {
  const payload = { chat_id: chatId, text: text.slice(0, 4000) };
  if (markup) payload.reply_markup = markup;
  const resp = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    throw new Error(`telegram sendMessage ${resp.status}: ${body.slice(0, 200)}`);
  }
}

async function answerCallback(env, cbId, text) {
  const resp = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/answerCallbackQuery`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ callback_query_id: cbId, text: String(text || "").slice(0, 190) }),
    },
  );
  if (!resp.ok) {
    // a missed answer just leaves the button spinner; worth no retry loop
  }
}
