#!/usr/bin/env node
/**
 * Minimal reverse proxy: local client -> this port -> configured upstream.
 * Only forwards HTTP; enforces a global concurrent-request cap (default 3).
 */
const http = require("node:http");
const https = require("node:https");
const { URL } = require("node:url");
const path = require("node:path");
const fs = require("node:fs");
const { ConcurrencyLimiter } = require("./limiter");

function loadConfig() {
  const file = path.join(__dirname, "config.json");
  let base = {};
  try {
    base = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (e) {
    if (e.code !== "ENOENT") throw e;
  }

  const port = Number(process.env.PROXY_PORT || base.port || 8788);
  const host = process.env.PROXY_HOST || base.host || "127.0.0.1";
  const upstream = process.env.PROXY_UPSTREAM || base.upstream || "http://127.0.0.1:8787";
  const maxConcurrent = Number(process.env.PROXY_MAX_CONCURRENT || base.maxConcurrent || 3);

  let upstreamUrl;
  try {
    upstreamUrl = new URL(upstream);
  } catch {
    throw new Error(`invalid upstream URL: ${upstream}`);
  }
  if (!["http:", "https:"].includes(upstreamUrl.protocol)) {
    throw new Error(`upstream must be http(s): ${upstream}`);
  }

  return { host, port, upstreamUrl, maxConcurrent: Math.max(1, maxConcurrent | 0) };
}

function copyHeaders(src, drop = new Set()) {
  const out = {};
  for (const [k, v] of Object.entries(src)) {
    if (drop.has(k.toLowerCase())) continue;
    out[k] = v;
  }
  return out;
}

function targetPath(req, upstreamUrl) {
  const base = upstreamUrl.pathname.replace(/\/$/, "");
  const pathWithQuery = req.url || "/";
  const [p, qs] = pathWithQuery.split("?");
  const joined = `${base}${p}` || "/";
  return qs ? `${joined}?${qs}` : joined;
}

function main() {
  const cfg = loadConfig();
  const limiter = new ConcurrencyLimiter(cfg.maxConcurrent);
  const isHttps = cfg.upstreamUrl.protocol === "https:";
  const agent = new (isHttps ? https : http).Agent({
    keepAlive: true,
    maxSockets: cfg.maxConcurrent,
  });

  const server = http.createServer((req, res) => {
    // Lightweight local status — bypasses the limiter.
    if (req.method === "GET" && req.url === "/proxy/status") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(
        JSON.stringify({
          active: limiter.active,
          max: limiter.max,
          available: limiter.available,
          upstream: cfg.upstreamUrl.origin,
        })
      );
      return;
    }

    let held = false;
    try {
      limiter.acquire();
      held = true;
    } catch (err) {
      res.writeHead(err.status || 429, {
        "content-type": "application/json",
        "retry-after": String(err.retryAfter || 1),
      });
      res.end(
        JSON.stringify({
          error: {
            message: err.message,
            type: "concurrency_limit",
            active: limiter.active,
            max: limiter.max,
          },
        })
      );
      return;
    }

    const released = () => {
      if (held) {
        held = false;
        limiter.release();
      }
    };

    const headers = copyHeaders(req.headers, new Set(["host", "connection", "content-length"]));
    headers.host = cfg.upstreamUrl.host;

    const options = {
      protocol: cfg.upstreamUrl.protocol,
      hostname: cfg.upstreamUrl.hostname,
      port: cfg.upstreamUrl.port || (isHttps ? 443 : 80),
      path: targetPath(req, cfg.upstreamUrl),
      method: req.method,
      headers,
      agent,
      timeout: 0,
    };

    const started = Date.now();
    const proxyReq = (isHttps ? https : http).request(options, (proxyRes) => {
      res.writeHead(proxyRes.statusCode || 502, proxyRes.headers);
      proxyRes.pipe(res);
      proxyRes.on("end", () => {
        console.log(
          `[proxy] ${req.method} ${req.url} -> ${proxyRes.statusCode} (${Date.now() - started}ms) active=${limiter.active}/${limiter.max}`
        );
        released();
      });
      proxyRes.on("error", () => {
        if (!res.headersSent) res.writeHead(502);
        res.end();
        released();
      });
    });

    proxyReq.on("timeout", () => {
      proxyReq.destroy(new Error("upstream timeout"));
    });

    proxyReq.on("error", (err) => {
      console.error(`[proxy] upstream error ${req.method} ${req.url}: ${err.message}`);
      if (!res.headersSent) {
        res.writeHead(502, { "content-type": "application/json" });
      }
      res.end(JSON.stringify({ error: { message: err.message, type: "upstream_error" } }));
      released();
    });

    req.pipe(proxyReq);
  });

  server.listen(cfg.port, cfg.host, () => {
    console.log(
      `[proxy] listening on http://${cfg.host}:${cfg.port} -> ${cfg.upstreamUrl.origin} (maxConcurrent=${cfg.maxConcurrent})`
    );
  });

  const shutdown = () => {
    console.log("[proxy] shutting down");
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 3000).unref();
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

main();
