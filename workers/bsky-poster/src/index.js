// RedBoxWatch → Bluesky poster.
//
// Hourly cron: fetch the site's JSON Feed, post any finding not yet in the
// KV "posted-ids" set, oldest first, capped per run. The first run ever
// (no KV key) records the current feed WITHOUT posting, so a fresh deploy
// never floods the account with backfill. State is written after each
// successful post, so a mid-run failure can't double-post.
//
// A manual trigger is exposed over HTTP for testing/instant posts:
//   curl -H "Authorization: Bearer $RUN_TOKEN" https://<worker-url>/

const BSKY_HOST = "https://bsky.social";

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(tick(env));
  },

  async fetch(request, env) {
    const got = request.headers.get("authorization") || "";
    if (!env.RUN_TOKEN || !(await timingSafeEqual(got, `Bearer ${env.RUN_TOKEN}`))) {
      return new Response("forbidden", { status: 403 });
    }
    try {
      return Response.json(await tick(env));
    } catch (err) {
      console.log(JSON.stringify({ event: "error", message: String(err) }));
      return Response.json({ error: String(err) }, { status: 500 });
    }
  },
};

async function tick(env) {
  const res = await fetch(env.FEED_URL, {
    headers: { "user-agent": "redboxwatch-bsky-poster (press@redboxwatch.org)" },
  });
  if (!res.ok) throw new Error(`feed fetch failed: ${res.status}`);
  const feed = await res.json(); // our own feed: small, bounded
  const items = feed.items ?? [];

  const stored = await env.STATE.get("posted-ids", "json");
  if (!stored) {
    await env.STATE.put("posted-ids", JSON.stringify(items.map((i) => i.id)));
    console.log(JSON.stringify({ event: "bootstrap", recorded: items.length }));
    return { bootstrapped: items.length, posted: 0 };
  }

  const posted = new Set(stored);
  // Feed is newest-first; post oldest-first so the timeline reads naturally.
  let fresh = items.filter((i) => i.id && !posted.has(i.id)).reverse();
  // A feed guid-scheme change makes every item look new at once (it happened:
  // cid/detection_id -> cid re-posted the whole backfill over a weekend).
  // But a wall of "new" is NOT always a migration: candidates routinely debut
  // in bulk after one review session, and absorbing those wholesale silently
  // never-posted them. The actual migration signature is per item: its
  // candidate prefix (the id before '#') is already in posted state under a
  // DIFFERENT suffix — a re-keyed known finding, not news. Absorb exactly
  // those; genuinely new candidates post normally (still capped per run).
  const floodLimit = Number(env.FLOOD_LIMIT || "15");
  let absorbed = 0;
  if (fresh.length > floodLimit) {
    const postedPrefixes = new Set([...posted].map((id) => String(id).split("#")[0]));
    const rekeyed = fresh.filter((i) => postedPrefixes.has(String(i.id).split("#")[0]));
    if (rekeyed.length) {
      for (const i of rekeyed) posted.add(i.id);
      await env.STATE.put("posted-ids", JSON.stringify([...posted]));
      console.log(JSON.stringify({ event: "migration-absorbed", count: rekeyed.length }));
      fresh = fresh.filter((i) => !posted.has(i.id));
      absorbed = rekeyed.length;
    }
  }
  const toPost = fresh.slice(0, Number(env.MAX_POSTS_PER_RUN || "5"));
  if (!toPost.length) return { posted: 0, pending: 0, absorbed };

  const session = await xrpc("com.atproto.server.createSession", null, {
    identifier: env.BSKY_HANDLE,
    password: env.BSKY_APP_PASSWORD,
  });

  let count = 0;
  let failed = 0;
  for (const item of toPost) {
    try {
      await xrpc("com.atproto.repo.createRecord", session.accessJwt, {
        repo: session.did,
        collection: "app.bsky.feed.post",
        record: {
          $type: "app.bsky.feed.post",
          // Bluesky rejects >300 graphemes; an overlong candidate title must
          // clip, not throw (one bad item used to wedge the poster on it
          // every hour, blocking everything queued behind it).
          text: clip(item.title || ""),
          createdAt: new Date().toISOString(),
          langs: ["en"],
          embed: {
            $type: "app.bsky.embed.external",
            external: {
              uri: item.url,
              title: item.title,
              description: clip(item.content_text || ""),
            },
          },
        },
      });
      posted.add(item.id);
      await env.STATE.put("posted-ids", JSON.stringify([...posted]));
      count += 1;
      console.log(JSON.stringify({ event: "posted", id: item.id, title: item.title }));
    } catch (err) {
      // Leave the item un-recorded (it stays pending for the next run) and
      // keep going: one rejected post must not abort the rest of the batch.
      failed += 1;
      console.log(JSON.stringify({ event: "post-failed", id: item.id,
                                   message: String(err) }));
    }
  }
  // pending = rate-capped remainder only; failed items are reported in
  // `failed` (they stay un-recorded and retry next run, but lumping them
  // into pending hid failures behind an expected-looking backlog number).
  return { posted: count, failed, pending: fresh.length - toPost.length, absorbed };
}

async function xrpc(method, jwt, body) {
  const res = await fetch(`${BSKY_HOST}/xrpc/${method}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(jwt ? { authorization: `Bearer ${jwt}` } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${method} -> ${res.status}: ${await res.text()}`);
  return res.json();
}

// Truncate at a word boundary with an ellipsis — never mid-word.
function clip(s, max = 290) {
  if (s.length <= max) return s;
  const cut = s.slice(0, max);
  const at = cut.lastIndexOf(" ");
  return (at > max / 2 ? cut.slice(0, at) : cut) + " …";
}

async function timingSafeEqual(a, b) {
  const enc = new TextEncoder();
  const [ab, bb] = [enc.encode(a), enc.encode(b)];
  if (ab.byteLength !== bb.byteLength) return false;
  return crypto.subtle.timingSafeEqual(ab, bb);
}
