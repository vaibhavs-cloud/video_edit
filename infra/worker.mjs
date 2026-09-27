// Telegram -> GitHub Actions relay (batch mode: nothing runs until `done`).
//
// POST /tg/<TELEGRAM_SECRET_PATH>  (+ X-Telegram-Bot-Api-Secret-Token check)
//   reply to a delivered video with text  -> dispatch kind=fix (state_ref + instruction)
//   `done` (or go/process)                -> dispatch staged video/link + images
//   `cancel`                              -> clear staging, dispatch nothing
//   text containing a URL                 -> stage the link, wait for `done`
//   photo / image file                   -> stage it (caption = label + time), wait
//   video attachment <= 20 MB             -> stage it, wait for `done`
//   video attachment > 20 MB              -> reply asking for a Drive link
//   anything else                         -> usage help
// GET /health -> 200
//
// Staging lives in KV (1h TTL): pending_video:<chat> + pending:<chat>.
// Image captions carry label + placement, e.g. `pyramid at 0:20`,
// `diagram 0:45-0:50`, `chart @1m20s` — any timestamp formatting works,
// the planner maps times to words and falls back to best-fit.
//
// Secrets (Cloudflare secret_text): TELEGRAM_BOT_TOKEN, TELEGRAM_SECRET_PATH,
// TELEGRAM_SECRET_TOKEN, GH_TOKEN, ALLOWED_CHAT_ID.
// Bindings: PENDING_IMAGES (KV namespace "vedit-pending-images").

const GITHUB_API = "https://api.github.com";
const TG_MAX_VIDEO_BYTES = 20 * 1024 * 1024;
const DONE_WORDS = new Set(["done", "go", "process", "process it", "start", "confirm"]);
const CANCEL_WORDS = new Set(["cancel", "clear", "reset"]);

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

  // 1) reply to a delivered message that carries a state ref -> correction
  const reply = msg.reply_to_message;
  if (reply) {
    const replied = String(reply.text || reply.caption || "");
    const stateRef = replied.match(/\b[0-9a-f]{8}\b/);
    if (stateRef) {
      if (!text) {
        await sendText(
          env,
          chatId,
          'send the correction as a reply, e.g. "place image 1 at 0:20"',
        );
        return;
      }
      await dispatch(env, {
        kind: "fix",
        chat_id: chatId,
        state_ref: stateRef[0],
        instruction: text,
        attachments: JSON.stringify(await takePending(env, chatId)),
      });
      await sendText(env, chatId, `queued fix ${stateRef[0]} — new cut coming`);
      return;
    }
    if (reply.from?.is_bot) {
      await sendText(
        env,
        chatId,
        "that message has no state ref — reply to a delivered video (its caption ends with `state xxxxxxxx`)",
      );
      return;
    }
  }

  // 2) staging commands (plain text only — never a reply/caption/media)
  const hasMedia = Boolean(msg.video || msg.photo || msg.document);
  const cmd = text.toLowerCase();
  if (!reply && !hasMedia) {
    if (DONE_WORDS.has(cmd)) {
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
    if (CANCEL_WORDS.has(cmd)) {
      await takePending(env, chatId);
      await clearVideo(env, chatId);
      await sendText(env, chatId, "cleared — staging empty. Send a video when ready.");
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

  // 6) anything else -> usage
  await sendText(
    env,
    chatId,
    "send a video or link (it waits here), then images with captions " +
      "(`pyramid at 0:20`), then `done` — v1 builds once, with everything placed. " +
      "Reply to a delivered video to correct it; `cancel` clears staging.",
  );
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
