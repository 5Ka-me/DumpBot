/** Vite dev-server plugin: answers /api/* from the in-memory MockApi (only for `vite` / `npm run dev`). */
import type { IncomingMessage, ServerResponse } from "node:http";
import type { Plugin } from "vite";
import { MockApi } from "./api.ts";

const MAX_BODY = 256 * 1024;

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_BODY) reject(new Error("too large"));
      else chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function send(res: ServerResponse, status: number, type: string, data: string): void {
  res.statusCode = status;
  res.setHeader("Content-Type", type);
  res.setHeader("Cache-Control", "no-store");
  res.end(data);
}

/** Answer delay, ms: a little latency makes loading states visible (MOCK_DELAY=0 turns it off). */
function delayFor(path: string): number {
  const base = Number(process.env.MOCK_DELAY ?? 250);
  if (path === "/api/ask") return base * 5;
  return base * (0.5 + Math.random());
}

export function mockApi(): Plugin {
  return {
    name: "dumpbot-mock-api",
    apply: "serve",
    configureServer(server) {
      const api = new MockApi();
      server.middlewares.use((req, res, next) => {
        const url = new URL(req.url ?? "/", "http://localhost");
        if (!url.pathname.startsWith("/api/")) {
          next();
          return;
        }
        void (async () => {
          let body: unknown;
          try {
            const text = await readBody(req);
            body = text ? JSON.parse(text) : undefined;
          } catch {
            send(res, 400, "application/json", JSON.stringify({ error: { code: "bad_request", message: "Тело запроса — не JSON" } }));
            return;
          }
          await new Promise((r) => setTimeout(r, delayFor(url.pathname)));
          const answer = api.handle({
            method: req.method ?? "GET",
            path: url.pathname,
            query: url.searchParams,
            authorization: req.headers.authorization,
            body,
          });
          if (answer.bytes) send(res, answer.status, answer.bytes.type, answer.bytes.data);
          else send(res, answer.status, "application/json; charset=utf-8", JSON.stringify(answer.json ?? null));
        })();
      });
      server.config.logger.info("  ➜  Mock API: /api/* is answered in memory (mock/api.ts)");
    },
  };
}
