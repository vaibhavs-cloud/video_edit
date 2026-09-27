// Telegram -> GitHub Actions relay.
//
// POST /tg/<TELEGRAM_SECRET_PATH>  (+ X-Telegram-Bot-Api-Secret-Token check)
//   reply to a delivered video with text  -> dispatch kind=fix (state_ref + instruction)
//   text containing a URL                 -> dispatch kind=new (url)
//   photo / image file                   -> staged in KV, attached to the next run
//   video attachment <= 20 MB             -> dispatch kind=new (video_file_id)
//   video attachment > 20 MB              -> reply asking for a Drive link
//   anything else                         -> usage help
// GET /health -> 200
//
// Secrets (Cloudflare secret_text): TELEGRAM_BOT_TOKEN, TELEGRAM_SECRET_PATH,
// TELEGRAM_SECRET_TOKEN, GH_TOKEN, ALLOWED_CHAT_ID.
// Bindings: PENDING_IMAGES (KV namespace "vedit-pending-images").

const GITHUB_API = "https://api.github.com";
const TG_MAX_VIDEO_BYTES = 20 * 1024 * 1024;

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

  // 2) URL in text -> Drive/direct link run
  const urlMatch = text.match(/https?:\/\/\S+/);
  if (urlMatch) {
    const prompt = text.replace(urlMatch[0], "").trim();
    await dispatch(env, {
      kind: "new",
      chat_id: chatId,
      url: urlMatch[0],
      prompt,
      attachments: JSON.stringify(await takePending(env, chatId)),
    });
    await sendText(env, chatId, "queued — processing your link now");
    return;
  }

  // 2b) photo / image file -> stage it for the next video/link run
  const photoSizes = Array.isArray(msg.photo) ? msg.photo : [];
  const photo = photoSizes.length ? photoSizes[photoSizes.length - 1] : null;
  const imageDoc =
    msg.document && /^image\//.test(msg.document.mime_type || "")
      ? msg.document
      : null;
  const image = photo || imageDoc;
  if (image && !msg.video) {
    const count = await pushPending(env, chatId, image.file_id);
    await sendText(
      env,
      chatId,
      `saved image ${count} — now send the video (or a link) and I'll place ${
        count === 1 ? "it" : "them"
      } in the edit`,
    );
    return;
  }

  // 3) video attachment -> file_id run (<= 20 MB)
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
    await dispatch(env, {
      kind: "new",
      chat_id: chatId,
      video_file_id: media.file_id,
      prompt: text,
      attachments: JSON.stringify(await takePending(env, chatId)),
    });
    await sendText(env, chatId, "queued — processing your video now");
    return;
  }

  // 4) anything else -> usage
  await sendText(
    env,
    chatId,
    "send images first (they'll be placed in the edit), then a Google Drive/direct video link, a video (≤20MB), or reply to a delivered video with a correction.",
  );
}

// Staged input images, buffered in KV between messages (1h TTL).
// Missing binding -> behave as if no images were staged.
function pendingKey(chatId) {
  return `pending:${chatId}`;
}

async function getPending(env, chatId) {
  try {
    if (!env.PENDING_IMAGES) return [];
    const raw = await env.PENDING_IMAGES.get(pendingKey(chatId));
    const list = JSON.parse(raw || "[]");
    return Array.isArray(list) ? list.filter((v) => typeof v === "string") : [];
  } catch {
    return [];
  }
}

async function pushPending(env, chatId, fileId) {
  const list = await getPending(env, chatId);
  list.push(fileId);
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
