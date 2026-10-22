# The page contract

The driver can render any page that follows this contract. It is small on purpose: a page answers one call per frame,
and everything else (encoding, segments, joining, the cloud) happens outside it. `page/render-page.js` implements the
page side for you; this document says what it does, so you can also implement it yourself.

## 1. Every frame is a pure function of its index

Frame `i` must look the same whenever, wherever and in whatever order it is rendered: the driver renders a film in
pieces, on several machines at once, resumes after interruptions, and joins the pieces by stream copy. So, inside
`draw(i)`:

- time is `i / fps`, never a clock (`performance.now()`, `Date.now()`, `requestAnimationFrame` timestamps);
- randomness is seeded by `i` (or by pixel position), never `Math.random()` without a seed;
- nothing carries over from the previous frame (no simulation stepped once per call, no accumulation buffers that
  depend on the order of calls). A simulation is fine if frame `i` can recompute or look up its state from `i` alone;
- everything is loaded before the page says it is ready.

The tests check this for the example: two pieces rendered by separate processes give byte-identical segments to one
run over the whole range (`tests/test_render.py`).

## 2. How the driver opens the page

```
<page>?render=1&w=<width>&h=<height>&format=<rgba|nv12|x2bgr10|p010>[&fps=<fps>][&<your parameters>]
```

`renderParams()` parses it: `{ render, width, height, fps, format, hdr, get(name, fallback) }`. Without `render`, the
page is being previewed by a person and can do whatever it likes (the example plays in real time). Your own parameters
come from `--param K=V` on the command line (`PARAMS="K=V"` in a cloud job).

The page is served by the driver from a folder on localhost (`--root`, by default the current folder when it holds the
page): relative URLs to scripts, shaders and data beside the page work. Nothing is fetched from the network unless the
page does it, and a page that does is no longer guaranteed to render the same everywhere.

## 3. What the page exposes

When it is ready, the page sets `window.__RENDER` and then `window.__RENDER_READY = true`. If it cannot render, it sets
`window.__RENDER_ERROR = "<reason>"` instead (`render-page.js` does this for anything thrown before ready).

| field | meaning |
|---|---|
| `version` | `1` |
| `fps`, `frameCount` | the timeline: frames `0 .. frameCount - 1` at `fps` |
| `width`, `height`, `format` | what the page renders; the driver checks they are what it asked for |
| `frameBytes` | the size of one frame's bytes (below) |
| `glRenderer` | `UNMASKED_RENDERER_WEBGL`: the driver refuses software rendering on paid machines (`--require-gpu`) |
| `hdr` | `{ peakNits, dither }` for HDR formats, else `null` |
| `info` | anything JSON-serialisable the page wants logged |
| `frameTo(i, url, next)` | render frame `i`, POST its bytes to `url`, start on frame `next` (below) |
| `audioRender(rate)`, `audioChunk(k)` | optional: the soundtrack as a WAV file, in 8 MB base64 pieces |

### `frameTo(i, url, next)`

1. If frame `i` is not already rendered (it was the previous call's `next`), render it into slot 0.
2. If `next` is given, render it into the other slot now, so the GPU works on it while frame `i` is read back.
3. Read frame `i`'s bytes back (this waits for the GPU).
4. POST them to `url` (`fetch(url, { method: 'POST', body: new Blob([bytes]) })`), without waiting for the upload;
   wait instead for the previous call's upload to have finished (a failure surfaces one call later). A `Blob` body
   travels through shared memory; an `ArrayBuffer` body took twenty times longer.
5. Return stage timings in milliseconds: `{ frame, render, speculate, read, post }` (the driver logs their means).

The driver keeps two calls queued, so the page always has the next frame to work on, and calls `gc()` every four
frames (it launches Chromium with `--expose-gc`): frames travel in large Blobs that otherwise pile up until uploads fail.

## 4. The bytes of a frame

Rows top first, no padding, little-endian. Limited ("TV") range, 4:2:0 chroma sited left (the H.264/HEVC default):
each chroma sample is the `[1 2 1] / 4` filter across even columns (edges clamped), averaged over the two rows.

| format | what | bytes per frame | ffmpeg `-pix_fmt` |
|---|---|---|---|
| `rgba` | 8-bit RGBA (alpha ignored); ffmpeg converts to BT.709 | `4·W·H` | `rgba` |
| `nv12` | 8-bit BT.709 Y'CbCr: `W·H` luma, then `W·H/2` interleaved Cb, Cr | `1.5·W·H` | `nv12` |
| `x2bgr10` | 10-bit RGB in 32-bit words, R in the low bits; ffmpeg converts to BT.2020 | `4·W·H` | `x2bgr10le` |
| `p010` | 10-bit BT.2020 Y'CbCr, 16-bit samples with the value in the top 10 bits, then 16 bytes of light levels | `3·W·H + 16` | `p010le` |

The `p010` trailer: the highest 10-bit code of any pixel's brightest channel (uint32), four zero bytes, and the frame
average of every pixel's brightest channel in nits (float64): the per-frame inputs of CTA-861.3's MaxCLL and MaxFALL.
NV12 needs `W` divisible by 4; P010 an even `W`; both an even `H`. The driver sends `nv12` and `p010` frames to the
encoder untouched, and lets ffmpeg convert `rgba` and `x2bgr10` frames (`--convert cpu`, or when the page's GPU
conversion fails the exactness check on a pod).

## 5. With `render-page.js`

```js
import { renderParams, exposeRenderer, encodeWav } from './render-page.js';

const P = renderParams();
const gl = canvas.getContext('webgl2', { antialias: false });
// ... compile, upload, load everything ...

function draw(i, target) {
  // target: { framebuffer, texture, width, height, hdr }, already bound, viewport set
  gl.useProgram(program);
  gl.uniform1f(uTime, i / 60);
  gl.uniform1i(uHdr, target.hdr ? 1 : 0);
  gl.drawArrays(gl.TRIANGLES, 0, 3);
}

if (P.render) exposeRenderer({ gl, fps: 60, frameCount: 3600, draw, audio: async (rate) => encodeWav(await render(rate)) });
```

- `draw(i, target)` draws frame `i` into `target.framebuffer`, or returns a `WebGLTexture` of its own that holds the
  frame (`W×H`, rows from the bottom as GL renders them). If your last pass usually renders to the canvas
  (`bindFramebuffer(..., null)`), bind `target.framebuffer` instead in render mode. Libraries that draw into their own
  render targets can return that target's texture.
- **SDR**: the target is RGBA8. Write display code values `0..1`, what the canvas would show (sRGB-encoded); the helper
  rounds them to 8 bits, so the RGBA and NV12 formats come from the same codes.
- **HDR**: the target is RGBA16F (`EXT_color_buffer_float` is required). Write PQ (SMPTE ST 2084) code values of BT.2020
  RGB, `0..1`. The helper clamps them at the PQ code of `peakNits` (default 1000, the mastering display's peak the
  master declares), adds a TPDF dither of ±1 LSB seeded by the frame index (`dither: false` turns it off; exact black
  stays black), and quantises to 10 bits.
- The helper saves and restores the GL state its own passes touch (framebuffers, program, vertex array, viewport,
  texture units 0 and 1, samplers, capabilities, pack state), so state you set once survives.
- `audio(rate)`, optional, returns the soundtrack as a WAV file's bytes; `encodeWav(audioBuffer)` makes one from an
  `OfflineAudioContext` render. `--audio auto` (the default) uses it; `--audio FILE` replaces it.

## 6. Canvas mode: any page that draws on a canvas

Most HTML animations draw on a canvas through a library (p5.js, Three.js, PixiJS, D3, a page an AI wrote) and never
touch a framebuffer. They can hand over the canvas instead of a WebGL context:

```js
import { renderParams, exposeRenderer } from './render-page.js';
const P = renderParams();
const canvas = document.querySelector('canvas');
if (P.render) { canvas.width = P.width; canvas.height = P.height; }   // frames are the canvas's own pixels
// ... set up the animation, but do not start its own clock or requestAnimationFrame loop when P.render ...
if (P.render) exposeRenderer({ canvas, fps: 60, frameCount: 600, draw: (i) => drawFrame(i / 60) });
```

`draw(i)` draws frame `i` on the canvas, synchronously, with whatever API the page uses (Canvas 2D, WebGL, WebGL2). The
helper then copies the canvas's 8-bit pixels into its own WebGL2 context (a GPU-side copy) and converts them as usual.
With a library, `drawFrame(t)` usually means: set the library's time or animation state from `t`, then call its render
or redraw once (for example p5's `redraw()` after `noLoop()`, or Three.js's `renderer.render(scene, camera)`).

- The canvas must be `width × height` (the URL's `w`, `h`); frames are opaque (paint a background).
- Canvas mode is SDR only: a canvas holds 8-bit display values. For HDR, render PQ values into the target (section 5).
- It gives exactly the frames the target path gives: `tests/test_exact.py` renders the example both ways and compares the
  bytes. `examples/canvas2d` is a page with no WebGL at all.

## 7. Why it converts on the GPU

On a cloud GPU the render is bound by the CPU, not the GPU: one worker's ffmpeg RGB to Y'CbCr conversion cost about as
much CPU as the browser's page thread, and metering HDR light in numpy another 70% of a core. Converting in a fragment
shader is nearly free for the GPU, and NV12 is 62% smaller than RGBA to read back and copy (P010: 25% smaller than
10-bit RGB). On the example page at 4K on an M2 MacBook Air, one worker renders 36 frames/s with the GPU conversion and
21 with ffmpeg's.

The shaders compute in float32 what a float64 reference computes: `python -m webgl2video exact <page>` renders the same
frames both ways and compares (in `tests/test_exact.py`, and on every pod before a render trusts the GPU path). ffmpeg's
own conversion (swscale) rounds differently: within a code in 8-bit luma, a few codes at sharp colour edges in chroma
(the driver tells it to site chroma left, as the GPU path does; its default, the centre, differed by up to 7 codes), and
in 10-bit up to 2 codes on about half the samples.
