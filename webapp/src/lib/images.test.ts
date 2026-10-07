/** The preview cache: bounded by bytes as well as by count, blobs freed when dropped. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MAX_BYTES, forgetImage, imageCacheSize, imageUrl, resetImages } from "./images";

const MB = 1024 * 1024;

let revoked: string[] = [];
let sizes: Record<string, number> = {};

beforeEach(() => {
  resetImages();
  revoked = [];
  sizes = {};
  let next = 0;
  vi.stubGlobal("URL", Object.assign(Object.create(URL), {
    createObjectURL: () => `blob:${++next}`,
    revokeObjectURL: (url: string) => void revoked.push(url),
  }));
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    const size = sizes[String(input)] ?? 1000;
    return new Response(new Uint8Array(size), { status: 200, headers: { "Content-Type": "image/png" } });
  });
});

afterEach(() => {
  resetImages();
  vi.unstubAllGlobals();
});

describe("imageUrl", () => {
  it("keeps the pictures under MAX_BYTES together, dropping the oldest and freeing their blobs", async () => {
    for (let i = 0; i < 4; i++) sizes[`/api/files/${i}/0`] = 8 * MB; // screenshots sent as files
    for (let i = 0; i < 4; i++) await imageUrl(`/api/files/${i}/0`);
    const { count, bytes } = imageCacheSize();
    expect(bytes).toBeLessThanOrEqual(MAX_BYTES);
    expect(count).toBe(Math.floor(MAX_BYTES / (8 * MB)));
    await vi.waitFor(() => expect(revoked).toEqual(["blob:1"])); // the oldest one
    expect(await imageUrl("/api/files/3/0")).toBe("blob:4"); // the newest is still cached
  });

  it("keeps one picture bigger than the limit while it is the newest", async () => {
    sizes["/api/files/1/0"] = MAX_BYTES + MB;
    expect(await imageUrl("/api/files/1/0")).toBe("blob:1");
    expect(imageCacheSize().count).toBe(1);
    await imageUrl("/api/files/2/0");
    expect(imageCacheSize()).toEqual({ count: 1, bytes: 1000 });
    await vi.waitFor(() => expect(revoked).toEqual(["blob:1"]));
  });

  it("frees the blob of a forgotten (broken) picture and loads it again", async () => {
    const first = await imageUrl("/api/files/1/0?size=thumb");
    forgetImage("/api/files/1/0?size=thumb");
    await vi.waitFor(() => expect(revoked).toEqual([first]));
    expect(imageCacheSize()).toEqual({ count: 0, bytes: 0 });
    expect(await imageUrl("/api/files/1/0?size=thumb")).not.toBe(first);
  });
});
