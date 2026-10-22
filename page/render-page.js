// render-page.js: the page side of the frame contract (docs/page-contract.md).
//
// A page that can be rendered frame by frame draws frame i when asked and calls exposeRenderer() once it is ready.
// The render driver (python -m webgl2video render) opens it as
//
//     <page>?render=1&w=<width>&h=<height>&format=<rgba|nv12|x2bgr10|p010>[&fps=<fps>][&<your own parameters>]
//
// and asks for frames through window.__RENDER. For each frame this module, on the GPU:
//   1. binds a W x H target (RGBA8 for SDR, RGBA16F for HDR) and calls your draw(i, target)
//   2. quantises the frame: 8-bit RGB (SDR), or 10-bit RGB of PQ code values with a TPDF dither (HDR); top row first
//   3. converts it, when asked, into what the encoder takes: NV12 (8-bit BT.709) or P010 (10-bit BT.2020), limited
//      range, 4:2:0 with the chroma sited left, and meters an HDR frame's light (MaxCLL, MaxFALL)
//   4. reads the bytes back and POSTs them to the driver's frame sink on localhost
// It keeps two slots, so frame i + 1 is drawn and converted while frame i is read back and sent.
//
//   import { renderParams, exposeRenderer } from './render-page.js';
//   const P = renderParams();          // { render, width, height, fps, format, hdr, get(name, fallback) }
//   ... create the WebGL2 context, compile, load everything ...
//   exposeRenderer({ gl, fps: 60, frameCount: 600, draw(i, target) { ... } });
//
// A page that draws on a canvas with any API (Canvas 2D, p5.js, Three.js, a page an AI wrote) can skip framebuffers:
//   exposeRenderer({ canvas, fps: 60, frameCount: 600, draw(i) { ... } });   // draw frame i on the canvas, at w x h
//
// draw(i, target) must be a pure function of i: no clock, no unseeded randomness, no state carried from frame to frame
// (the driver renders any range of frames, in pieces, on many machines, and joins them). target is
// { framebuffer, texture, width, height, hdr }, bound, with the viewport set to it: draw frame i into it (SDR: display
// code values 0..1, what a canvas would show; HDR: PQ code values of BT.2020 RGB, 0..1), or return a WebGLTexture of
// your own that holds the frame (W x H, rows from the bottom, as GL renders them). Alpha is ignored.
//
// Why the conversion happens here: on a cloud GPU the render is bound by the CPU, not the GPU. Converting RGB to
// Y'CbCr in ffmpeg (and metering HDR light in numpy) cost a render worker more CPU than the page itself, and the 4:2:0
// frame is 62% (SDR) or 25% (HDR) smaller to read back. The conversion matches a float64 reference to within one
// code (`python -m webgl2video exact` checks it on the GPU at hand; tests/test_exact.py); ffmpeg's own (swscale, the
// --convert cpu path) rounds differently by a code or two.

export const FORMATS = {
  rgba: { hdr: false, bytes: (w, h) => w * h * 4 }, // 8-bit RGBA, top row first (ffmpeg's rgba)
  nv12: { hdr: false, bytes: (w, h) => (w * h * 3) / 2 }, // 8-bit BT.709 Y'CbCr 4:2:0 (ffmpeg's nv12)
  x2bgr10: { hdr: true, bytes: (w, h) => w * h * 4 }, // 10-bit RGB in 32-bit words, R lowest (ffmpeg's x2bgr10le)
  p010: { hdr: true, bytes: (w, h) => w * h * 3 + 16 }, // 10-bit BT.2020 Y'CbCr 4:2:0 (p010le) + 16 bytes of light
};

/** The render parameters in the page's URL. `render` is false when the page is opened for a preview. */
export function renderParams(search = typeof location !== 'undefined' ? location.search : '') {
  const q = new URLSearchParams(search);
  const int = (k) => (/^\d+$/.test(q.get(k) || '') ? parseInt(q.get(k), 10) : null);
  const format = q.get('format') || 'rgba';
  const fps = q.has('fps') && Number(q.get('fps')) > 0 ? Number(q.get('fps')) : null;
  return {
    render: q.has('render') && q.get('render') !== '0',
    width: int('w'),
    height: int('h'),
    fps,
    format,
    hdr: !!(FORMATS[format] && FORMATS[format].hdr),
    get: (k, fallback = null) => (q.has(k) ? q.get(k) : fallback),
  };
}

/** Stop the render with a reason the driver prints (anything thrown before the page is ready lands here too). */
export function renderFail(reason) {
  if (typeof window === 'undefined' || window.__RENDER_READY || window.__RENDER_ERROR) return;
  window.__RENDER_ERROR = String((reason && reason.message) || reason || 'unknown error');
}

if (typeof window !== 'undefined') {
  window.addEventListener('error', (e) => renderFail(e.error || e.message));
  window.addEventListener('unhandledrejection', (e) => renderFail(e.reason));
}

/** A stereo WAV file of an AudioBuffer (from an OfflineAudioContext): 16-bit PCM, or 32-bit float. */
export function encodeWav(buffer, { float = false } = {}) {
  const ch = buffer.numberOfChannels, n = buffer.length, bps = float ? 4 : 2;
  const data = ch * n * bps, out = new DataView(new ArrayBuffer(44 + data));
  const str = (o, s) => [...s].forEach((c, k) => out.setUint8(o + k, c.charCodeAt(0)));
  str(0, 'RIFF'); out.setUint32(4, 36 + data, true); str(8, 'WAVE'); str(12, 'fmt ');
  out.setUint32(16, 16, true); out.setUint16(20, float ? 3 : 1, true); out.setUint16(22, ch, true);
  out.setUint32(24, buffer.sampleRate, true); out.setUint32(28, buffer.sampleRate * ch * bps, true);
  out.setUint16(32, ch * bps, true); out.setUint16(34, bps * 8, true); str(36, 'data'); out.setUint32(40, data, true);
  const chans = [...Array(ch)].map((_, c) => buffer.getChannelData(c));
  for (let i = 0, o = 44; i < n; i++) {
    for (let c = 0; c < ch; c++, o += bps) {
      const v = Math.max(-1, Math.min(1, chans[c][i]));
      if (float) out.setFloat32(o, v, true);
      else out.setInt16(o, Math.round(v < 0 ? v * 32768 : v * 32767), true);
    }
  }
  return new Uint8Array(out.buffer);
}

const HEADER = `#version 300 es
precision highp float;
precision highp int;
precision highp sampler2D;
`;

// One triangle that covers the viewport; no vertex buffers.
const FULLSCREEN_VS = `#version 300 es
void main() {
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;

// The frame as drawn (rows from the bottom) into 8-bit codes, top row first, so one plain readback hands the rows
// over in the order a video frame has them.
const QUANT8_FS = HEADER + `
out vec4 o;
uniform sampler2D uSrc;
uniform int uH;
void main() {
  ivec2 p = ivec2(gl_FragCoord.xy);
  vec3 c = texelFetch(uSrc, ivec2(p.x, uH - 1 - p.y), 0).rgb;
  o = vec4(floor(clamp(c, 0.0, 1.0) * 255.0 + 0.5) / 255.0, 1.0);
}`;

// HDR: the PQ code values as drawn into 10-bit codes, top row first, with a TPDF dither (+-1 LSB of 10 bits, seeded by
// the frame index, so a frame is the same whenever it is rendered) that never bands; none on exact black, which stays
// 0. Nothing passes the peak the mastering display is declared with.
const QUANT10_FS = HEADER + `
out vec4 o;
uniform sampler2D uSrc;
uniform int uH;
uniform uint uSeed;
uniform float uPeak; // the PQ code value of the peak
uniform int uDither;
uint pcg(uint v) {
  uint s = v * 747796405u + 2891336453u;
  uint w = ((s >> ((s >> 28u) + 4u)) ^ s) * 277803737u;
  return (w >> 22u) ^ w;
}
vec3 rand3(uvec2 p, uint seed) {
  uint a = pcg(p.x ^ pcg(p.y ^ pcg(seed)));
  uint b = pcg(a + 0x9E3779B9u);
  uint c = pcg(b + 0x7F4A7C15u);
  return vec3(float(a), float(b), float(c)) * (1.0 / 4294967295.0);
}
void main() {
  ivec2 p = ivec2(gl_FragCoord.xy);
  vec3 q = clamp(texelFetch(uSrc, ivec2(p.x, uH - 1 - p.y), 0).rgb, 0.0, uPeak);
  if (uDither != 0) {
    vec3 n = rand3(uvec2(p), uSeed ^ 0xA511E9B3u);
    vec3 n2 = rand3(uvec2(p), uSeed * 2654435761u + 17u);
    float amp = smoothstep(0.0, 1.5 / 1023.0, max(q.r, max(q.g, q.b)));
    q += (n + n2 - 1.0) * (amp / 1023.0);
  }
  o = vec4(floor(clamp(q, 0.0, uPeak) * 1023.0 + 0.5) / 1023.0, 1.0);
}`;

// NV12 packed into an RGBA8 target a quarter of the frame wide and one and a half times as tall, so one plain readback
// hands the bytes over in order: rows 0 to H-1, luma (four pixels a texel); rows H to 1.5H-1, chroma (two Cb, Cr pairs
// a texel). BT.709 limited range from the 8-bit codes (code / 255); the chroma sited left (H.264's default,
// chroma_sample_loc_type 0): the [1 2 1] / 4 filter across, the mean of two rows.
const NV12_FS = HEADER + `
out vec4 o;
uniform sampler2D uRgb; // the 8-bit frame, top row first
uniform ivec2 uSize;
vec3 rgbAt(int x, int y) {
  return floor(texelFetch(uRgb, ivec2(clamp(x, 0, uSize.x - 1), y), 0).rgb * 255.0 + 0.5) / 255.0;
}
float lumaOf(vec3 e) { return dot(e, vec3(0.2126, 0.7152, 0.0722)); }
float code8(float v) { return clamp(floor(v + 0.5), 0.0, 255.0); }
vec2 chroma(int cx, int r) {
  int x = 2 * cx;
  vec3 e = vec3(0.0);
  for (int dy = 0; dy < 2; dy++)
    e += 0.25 * rgbAt(x - 1, r + dy) + 0.5 * rgbAt(x, r + dy) + 0.25 * rgbAt(x + 1, r + dy);
  e *= 0.5;
  float yl = lumaOf(e);
  return vec2(code8(128.0 + 224.0 * (e.b - yl) / 1.8556), code8(128.0 + 224.0 * (e.r - yl) / 1.5748));
}
void main() {
  ivec2 p = ivec2(gl_FragCoord.xy);
  if (p.y < uSize.y) {
    int x = 4 * p.x;
    o = vec4(code8(16.0 + 219.0 * lumaOf(rgbAt(x, p.y))), code8(16.0 + 219.0 * lumaOf(rgbAt(x + 1, p.y))),
             code8(16.0 + 219.0 * lumaOf(rgbAt(x + 2, p.y))), code8(16.0 + 219.0 * lumaOf(rgbAt(x + 3, p.y)))) / 255.0;
  } else {
    int r = 2 * (p.y - uSize.y);
    o = vec4(chroma(2 * p.x, r), chroma(2 * p.x + 1, r)) / 255.0;
  }
}`;

// P010 (ffmpeg's p010le) packed into an RGBA8 target half the frame wide and one and a half times as tall. Rows 0 to
// H-1: luma, each texel two pixels (two little-endian 16-bit samples, the 10-bit value in the top bits). Rows H to
// 1.5H-1: chroma, each texel one (Cb, Cr) pair. BT.2020 non-constant luminance, limited range, from the 10-bit codes
// (code / 1023); the chroma sited left (HEVC's default): the [1 2 1] / 4 filter across, the mean of the two rows.
const P010_FS = HEADER + `
out vec4 o;
uniform sampler2D uRgb; // the 10-bit frame (RGB10_A2, top row first)
uniform ivec2 uSize;
vec3 rgbAt(int x, int y) {
  return floor(texelFetch(uRgb, ivec2(clamp(x, 0, uSize.x - 1), y), 0).rgb * 1023.0 + 0.5) / 1023.0;
}
float lumaOf(vec3 e) { return dot(e, vec3(0.2627, 0.6780, 0.0593)); }
float code10(float v) { return clamp(floor(v + 0.5), 0.0, 1023.0); }
vec2 bytes(float c) { return vec2(mod(c, 4.0) * 64.0, floor(c / 4.0)) / 255.0; }
void main() {
  ivec2 p = ivec2(gl_FragCoord.xy);
  if (p.y < uSize.y) {
    float y0 = code10(64.0 + 876.0 * lumaOf(rgbAt(2 * p.x, p.y)));
    float y1 = code10(64.0 + 876.0 * lumaOf(rgbAt(2 * p.x + 1, p.y)));
    o = vec4(bytes(y0), bytes(y1));
  } else {
    int r = 2 * (p.y - uSize.y), x = 2 * p.x;
    vec3 e = vec3(0.0);
    for (int dy = 0; dy < 2; dy++)
      e += 0.25 * rgbAt(x - 1, r + dy) + 0.5 * rgbAt(x, r + dy) + 0.25 * rgbAt(x + 1, r + dy);
    e *= 0.5;
    float yl = lumaOf(e);
    o = vec4(bytes(code10(512.0 + 896.0 * (e.b - yl) / 1.8814)), bytes(code10(512.0 + 896.0 * (e.r - yl) / 1.4746)));
  }
}`;

// The light meter (CTA-861.3's MaxCLL and MaxFALL, per frame), in two reductions of 16 x 16 blocks: each pixel's
// brightest channel as a 10-bit code, its luminance from the PQ table, and per block the highest code and the sum.
const METER1_FS = HEADER + `
out vec4 o;
uniform sampler2D uRgb;  // the 10-bit frame
uniform sampler2D uNits; // 1024 x 1: each code's nits
uniform ivec2 uSize;
void main() {
  ivec2 b = ivec2(gl_FragCoord.xy) * 16;
  float top = 0.0, sum = 0.0;
  for (int y = 0; y < 16; y++) {
    if (b.y + y >= uSize.y) break;
    for (int x = 0; x < 16; x++) {
      if (b.x + x >= uSize.x) break;
      vec3 c = floor(texelFetch(uRgb, b + ivec2(x, y), 0).rgb * 1023.0 + 0.5);
      float m = max(c.r, max(c.g, c.b));
      top = max(top, m);
      sum += texelFetch(uNits, ivec2(int(m), 0), 0).r;
    }
  }
  o = vec4(top, sum, 0.0, 1.0);
}`;
const METER2_FS = HEADER + `
out vec4 o;
uniform sampler2D uIn; // the first reduction
void main() {
  ivec2 b = ivec2(gl_FragCoord.xy) * 16, n = textureSize(uIn, 0);
  float top = 0.0, sum = 0.0;
  for (int y = 0; y < 16; y++) {
    if (b.y + y >= n.y) break;
    for (int x = 0; x < 16; x++) {
      if (b.x + x >= n.x) break;
      vec2 v = texelFetch(uIn, b + ivec2(x, y), 0).rg;
      top = max(top, v.x);
      sum += v.y;
    }
  }
  o = vec4(top, sum, 0.0, 1.0);
}`;

/** SMPTE ST 2084: a PQ code value (0..1) as nits, and nits as a PQ code value. */
const M1 = 2610 / 16384, M2 = (2523 / 4096) * 128, C1 = 3424 / 4096, C2 = (2413 / 4096) * 32, C3 = (2392 / 4096) * 32;
export function pqDecode(v) {
  const p = Math.pow(Math.min(1, Math.max(0, v)), 1 / M2);
  return 10000 * Math.pow(Math.max(p - C1, 0) / (C2 - C3 * p), 1 / M1);
}
export function pqEncode(nits) {
  const y = Math.pow(Math.min(1, Math.max(0, nits / 10000)), M1);
  return Math.pow((C1 + C2 * y) / (1 + C3 * y), M2);
}

function compile(gl, fs, name) {
  const sh = (type, src) => {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS) && !gl.isContextLost()) throw new Error(`render-page: ${name}: ${gl.getShaderInfoLog(s)}`);
    return s;
  };
  const p = gl.createProgram();
  const vs = sh(gl.VERTEX_SHADER, FULLSCREEN_VS), f = sh(gl.FRAGMENT_SHADER, fs);
  gl.attachShader(p, vs);
  gl.attachShader(p, f);
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS) && !gl.isContextLost()) throw new Error(`render-page: ${name}: ${gl.getProgramInfoLog(p)}`);
  gl.deleteShader(vs);
  gl.deleteShader(f);
  const loc = {};
  return { program: p, u: (k) => (k in loc ? loc[k] : (loc[k] = gl.getUniformLocation(p, k))) };
}

function makeTarget(gl, w, h, internal) {
  const t = { width: w, height: h, texture: gl.createTexture(), framebuffer: gl.createFramebuffer() };
  gl.bindTexture(gl.TEXTURE_2D, t.texture);
  gl.texStorage2D(gl.TEXTURE_2D, 1, internal, w, h);
  for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.NEAREST], [gl.TEXTURE_MAG_FILTER, gl.NEAREST],
    [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
  gl.bindFramebuffer(gl.FRAMEBUFFER, t.framebuffer);
  gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t.texture, 0);
  const status = gl.checkFramebufferStatus(gl.FRAMEBUFFER);
  if (status !== gl.FRAMEBUFFER_COMPLETE) throw new Error(`render-page: no ${w}x${h} render target of format 0x${internal.toString(16)} here (0x${status.toString(16)})`);
  return t;
}

// The GL state the helper's own passes touch, saved before them and put back after, so a page that sets its state
// once keeps it.
const CAPS = ['BLEND', 'CULL_FACE', 'DEPTH_TEST', 'DITHER', 'POLYGON_OFFSET_FILL', 'RASTERIZER_DISCARD',
  'SAMPLE_ALPHA_TO_COVERAGE', 'SAMPLE_COVERAGE', 'SCISSOR_TEST', 'STENCIL_TEST'];
function saveState(gl) {
  const s = {
    caps: CAPS.map((c) => gl.isEnabled(gl[c])),
    draw: gl.getParameter(gl.DRAW_FRAMEBUFFER_BINDING), read: gl.getParameter(gl.READ_FRAMEBUFFER_BINDING),
    viewport: gl.getParameter(gl.VIEWPORT), program: gl.getParameter(gl.CURRENT_PROGRAM),
    vao: gl.getParameter(gl.VERTEX_ARRAY_BINDING), mask: gl.getParameter(gl.COLOR_WRITEMASK),
    active: gl.getParameter(gl.ACTIVE_TEXTURE), pack: gl.getParameter(gl.PIXEL_PACK_BUFFER_BINDING),
    packs: [gl.PACK_ALIGNMENT, gl.PACK_ROW_LENGTH, gl.PACK_SKIP_PIXELS, gl.PACK_SKIP_ROWS].map((k) => [k, gl.getParameter(k)]),
    units: [0, 1].map((u) => {
      gl.activeTexture(gl.TEXTURE0 + u);
      return [gl.getParameter(gl.TEXTURE_BINDING_2D), gl.getParameter(gl.SAMPLER_BINDING)];
    }),
  };
  for (const c of CAPS) gl.disable(gl[c]);
  gl.colorMask(true, true, true, true);
  gl.bindBuffer(gl.PIXEL_PACK_BUFFER, null);
  for (const [k, v] of [[gl.PACK_ALIGNMENT, 4], [gl.PACK_ROW_LENGTH, 0], [gl.PACK_SKIP_PIXELS, 0], [gl.PACK_SKIP_ROWS, 0]]) gl.pixelStorei(k, v);
  for (const u of [0, 1]) gl.bindSampler(u, null);
  return s;
}
function restoreState(gl, s) {
  CAPS.forEach((c, k) => (s.caps[k] ? gl.enable(gl[c]) : gl.disable(gl[c])));
  gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, s.draw);
  gl.bindFramebuffer(gl.READ_FRAMEBUFFER, s.read);
  gl.viewport(s.viewport[0], s.viewport[1], s.viewport[2], s.viewport[3]);
  gl.useProgram(s.program);
  gl.bindVertexArray(s.vao);
  gl.colorMask(s.mask[0], s.mask[1], s.mask[2], s.mask[3]);
  gl.bindBuffer(gl.PIXEL_PACK_BUFFER, s.pack);
  for (const [k, v] of s.packs) gl.pixelStorei(k, v);
  s.units.forEach(([tex, sampler], u) => {
    gl.activeTexture(gl.TEXTURE0 + u);
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.bindSampler(u, sampler);
  });
  gl.activeTexture(s.active);
}

/** The GPU side: the draw target, two output slots, the passes and the readback. */
function createConverter(gl, { width: W, height: H, format, peakNits, dither }) {
  const hdr = FORMATS[format].hdr;
  const saved = saveState(gl);
  try {
    const vao = gl.createVertexArray();
    const P = {
      quant: compile(gl, hdr ? QUANT10_FS : QUANT8_FS, hdr ? 'quantise-10' : 'quantise-8'),
      ...(format === 'nv12' ? { yuv: compile(gl, NV12_FS, 'nv12') } : {}),
      ...(format === 'p010' ? { yuv: compile(gl, P010_FS, 'p010'), m1: compile(gl, METER1_FS, 'meter-1'), m2: compile(gl, METER2_FS, 'meter-2') } : {}),
    };
    const drawn = makeTarget(gl, W, H, hdr ? gl.RGBA16F : gl.RGBA8); // what draw() renders into (shared by both slots)
    const rgb = [0, 1].map(() => makeTarget(gl, W, H, hdr ? gl.RGB10_A2 : gl.RGBA8)); // quantised, top row first
    const yuv = format === 'nv12' ? [0, 1].map(() => makeTarget(gl, W / 4, H * 1.5, gl.RGBA8))
      : format === 'p010' ? [0, 1].map(() => makeTarget(gl, W / 2, H * 1.5, gl.RGBA8)) : null;
    let m1 = null, m2 = null, m2buf = null, nits = null;
    if (format === 'p010') {
      m1 = makeTarget(gl, Math.ceil(W / 16), Math.ceil(H / 16), gl.RGBA32F);
      m2 = [0, 1].map(() => makeTarget(gl, Math.ceil(m1.width / 16), Math.ceil(m1.height / 16), gl.RGBA32F));
      m2buf = new Float32Array(m2[0].width * m2[0].height * 4);
      const table = new Float32Array(1024);
      for (let c = 0; c < 1024; c++) table[c] = pqDecode(c / 1023);
      nits = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, nits);
      gl.texStorage2D(gl.TEXTURE_2D, 1, gl.R32F, 1024, 1);
      const unpack = [gl.UNPACK_ALIGNMENT, gl.UNPACK_ROW_LENGTH, gl.UNPACK_SKIP_PIXELS, gl.UNPACK_SKIP_ROWS, gl.UNPACK_FLIP_Y_WEBGL]
        .map((k) => [k, gl.getParameter(k)]);
      const unpackBuffer = gl.getParameter(gl.PIXEL_UNPACK_BUFFER_BINDING);
      gl.bindBuffer(gl.PIXEL_UNPACK_BUFFER, null);
      for (const [k] of unpack) gl.pixelStorei(k, k === gl.UNPACK_ALIGNMENT ? 4 : 0);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, 1024, 1, gl.RED, gl.FLOAT, table);
      for (const [k, v] of unpack) gl.pixelStorei(k, v);
      gl.bindBuffer(gl.PIXEL_UNPACK_BUFFER, unpackBuffer);
      for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.NEAREST], [gl.TEXTURE_MAG_FILTER, gl.NEAREST]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
    }
    const peak = pqEncode(peakNits);
    const bufs = [null, null];

    const pass = (prog, target, textures, uniforms = () => {}) => {
      gl.useProgram(prog.program);
      gl.bindFramebuffer(gl.FRAMEBUFFER, target.framebuffer);
      gl.viewport(0, 0, target.width, target.height);
      textures.forEach(([name, tex], unit) => {
        gl.activeTexture(gl.TEXTURE0 + unit);
        gl.bindTexture(gl.TEXTURE_2D, tex);
        gl.uniform1i(prog.u(name), unit);
      });
      uniforms(prog);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    };

    /** The frame drawn (in `src`, rows from the bottom) through the passes into slot `slot`; i seeds the dither. */
    function convert(src, slot, i) {
      const s = saveState(gl);
      try {
        gl.bindVertexArray(vao);
        pass(P.quant, rgb[slot], [['uSrc', src]], (p) => {
          gl.uniform1i(p.u('uH'), H);
          if (hdr) {
            gl.uniform1ui(p.u('uSeed'), i >>> 0);
            gl.uniform1f(p.u('uPeak'), peak);
            gl.uniform1i(p.u('uDither'), dither ? 1 : 0);
          }
        });
        if (yuv) pass(P.yuv, yuv[slot], [['uRgb', rgb[slot].texture]], (p) => gl.uniform2i(p.u('uSize'), W, H));
        if (format === 'p010') {
          pass(P.m1, m1, [['uRgb', rgb[slot].texture], ['uNits', nits]], (p) => gl.uniform2i(p.u('uSize'), W, H));
          pass(P.m2, m2[slot], [['uIn', m1.texture]]);
        }
      } finally {
        restoreState(gl, s);
      }
    }

    /** Slot `slot`'s bytes, as the driver's format names them (waits for the GPU to finish that frame). */
    function read(slot) {
      // Errors some other pass left behind are not the readback's, but out of memory means a pass drew nothing: that
      // frame is not the film's. GL keeps one flag per kind of error, so all are drained.
      for (let k = 0, e = gl.getError(); e !== gl.NO_ERROR && k < 8; k++, e = gl.getError()) {
        if (e === gl.OUT_OF_MEMORY) throw new Error('render-page: the GPU ran out of memory (GL_OUT_OF_MEMORY) while making this frame');
        console.warn(`[render-page] GL error 0x${e.toString(16)} before the readback`);
      }
      const s = saveState(gl);
      try {
        if (format === 'rgba') {
          if (!bufs[slot]) bufs[slot] = new Uint8Array(W * H * 4);
          gl.bindFramebuffer(gl.READ_FRAMEBUFFER, rgb[slot].framebuffer);
          gl.readPixels(0, 0, W, H, gl.RGBA, gl.UNSIGNED_BYTE, bufs[slot]);
        } else if (format === 'x2bgr10') {
          if (!bufs[slot]) bufs[slot] = new Uint32Array(W * H);
          gl.bindFramebuffer(gl.READ_FRAMEBUFFER, rgb[slot].framebuffer);
          gl.readPixels(0, 0, W, H, gl.RGBA, gl.UNSIGNED_INT_2_10_10_10_REV, bufs[slot]);
        } else if (format === 'nv12') {
          if (!bufs[slot]) bufs[slot] = new Uint8Array((W * H * 3) / 2);
          gl.bindFramebuffer(gl.READ_FRAMEBUFFER, yuv[slot].framebuffer);
          gl.readPixels(0, 0, W / 4, H * 1.5, gl.RGBA, gl.UNSIGNED_BYTE, bufs[slot]);
        } else {
          const n = W * H * 3; // p010le: 2 bytes a pixel of luma, 1 of chroma; then the light levels
          if (!bufs[slot]) bufs[slot] = new Uint8Array(n + 16);
          gl.bindFramebuffer(gl.READ_FRAMEBUFFER, yuv[slot].framebuffer);
          gl.readPixels(0, 0, W / 2, H * 1.5, gl.RGBA, gl.UNSIGNED_BYTE, bufs[slot], 0);
          gl.bindFramebuffer(gl.READ_FRAMEBUFFER, m2[slot].framebuffer);
          gl.readPixels(0, 0, m2[slot].width, m2[slot].height, gl.RGBA, gl.FLOAT, m2buf);
          let top = 0, sum = 0;
          for (let k = 0; k < m2buf.length; k += 4) { top = Math.max(top, m2buf[k]); sum += m2buf[k + 1]; }
          // the brightest pixel's brightest channel as a 10-bit code (uint32), 4 bytes of zeros, the frame average of
          // each pixel's brightest channel in nits (float64), little-endian
          const tail = new DataView(bufs[slot].buffer, n, 16);
          tail.setUint32(0, Math.round(top), true);
          tail.setUint32(4, 0, true);
          tail.setFloat64(8, sum / (W * H), true);
        }
      } finally {
        restoreState(gl, s);
      }
      const err = gl.getError();
      if (err !== gl.NO_ERROR) throw new Error(`render-page: the ${format} readback failed (GL error 0x${err.toString(16)})`);
      return bufs[slot] instanceof Uint8Array ? bufs[slot] : new Uint8Array(bufs[slot].buffer);
    }

    function dispose() {
      for (const t of [drawn, ...rgb, ...(yuv || []), ...(m2 || []), m1]) {
        if (!t) continue;
        gl.deleteFramebuffer(t.framebuffer);
        gl.deleteTexture(t.texture);
      }
      if (nits) gl.deleteTexture(nits);
      for (const k in P) gl.deleteProgram(P[k].program);
      gl.deleteVertexArray(vao);
    }

    return { drawn, convert, read, dispose };
  } finally {
    restoreState(gl, saved);
  }
}

function toBase64(bytes) {
  let bin = '';
  for (let k = 0; k < bytes.length; k += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(k, k + 0x8000));
  return btoa(bin);
}

const AUDIO_CHUNK = 8 * 1024 * 1024; // the soundtrack crosses to the driver in pieces of this size (as base64)

/**
 * Make the page renderable: window.__RENDER, then window.__RENDER_READY = true.
 *
 *   gl          a WebGL2 context: draw(i, target) renders frame i into target (or returns a texture of its own)
 *   canvas      or, instead of gl, any canvas the page draws its frames on (2D, WebGL, p5.js, Three.js...): draw(i)
 *               draws frame i on it, synchronously, at width x height, and the helper copies it from there (SDR only)
 *   fps         frames per second of the timeline; frameCount, how many frames it has
 *   draw        (i, target) => undefined | WebGLTexture with gl; (i) => undefined with canvas
 *   width, height, format    default: the URL's w, h and format
 *   peakNits    HDR: the mastering display's peak; nothing brighter is written (default 1000)
 *   dither      HDR: a TPDF dither before the 10-bit quantisation (default true)
 *   audio       optional: async (sampleRate) => the soundtrack as a WAV file's bytes (encodeWav helps)
 *   info        optional: anything JSON the driver should log
 */
export function exposeRenderer(options) {
  try {
    return expose(options);
  } catch (e) {
    renderFail(e);
    throw e;
  }
}

function expose({ gl, canvas = null, fps, frameCount, draw, width, height, format, peakNits = 1000, dither = true, audio = null, info = null }) {
  const P = renderParams();
  if (canvas) {
    // Canvas mode: the page draws on a canvas of its own, with any API; the helper copies each frame from it into its
    // own WebGL2 context (a GPU-side copy of the canvas's 8-bit pixels) and converts it there.
    if (!(typeof HTMLCanvasElement !== 'undefined' && canvas instanceof HTMLCanvasElement)) throw new Error('render-page: canvas is not a <canvas> element');
    if (FORMATS[format ?? P.format] && FORMATS[format ?? P.format].hdr) throw new Error('render-page: a canvas holds 8-bit SDR pixels: for HDR, draw PQ values into the target with gl (docs/page-contract.md)');
    width = width ?? P.width ?? canvas.width;
    height = height ?? P.height ?? canvas.height;
    gl = document.createElement('canvas').getContext('webgl2', { antialias: false, alpha: false, depth: false, powerPreference: 'high-performance' });
    if (!gl) throw new Error('render-page: WebGL2 is not available here');
  }
  if (typeof WebGL2RenderingContext === 'undefined' || !(gl instanceof WebGL2RenderingContext)) throw new Error('render-page: exposeRenderer needs a WebGL2 context (gl) or a canvas');
  if (typeof draw !== 'function') throw new Error('render-page: exposeRenderer needs draw(i, target)');
  if (!(fps > 0)) throw new Error(`render-page: fps ${fps}`);
  if (!(Number.isInteger(frameCount) && frameCount > 0)) throw new Error(`render-page: frameCount ${frameCount}`);
  const W = Math.round(width ?? P.width ?? gl.drawingBufferWidth);
  const H = Math.round(height ?? P.height ?? gl.drawingBufferHeight);
  format = format ?? P.format;
  const F = FORMATS[format];
  if (!F) throw new Error(`render-page: unknown format ${format} (${Object.keys(FORMATS).join(', ')})`);
  if (!(W > 0 && H > 0)) throw new Error(`render-page: frame size ${W}x${H}`);
  if (format === 'nv12' && (W % 4 || H % 2)) throw new Error(`render-page: NV12 needs a width divisible by 4 and an even height, not ${W}x${H}`);
  if (format === 'p010' && (W % 2 || H % 2)) throw new Error(`render-page: P010 needs an even width and height, not ${W}x${H}`);
  const max = Math.min(gl.getParameter(gl.MAX_TEXTURE_SIZE), gl.getParameter(gl.MAX_RENDERBUFFER_SIZE));
  if (W > max || H * 1.5 > max) throw new Error(`render-page: ${W}x${H} is larger than this GPU's ${max}-pixel limit`);
  if (F.hdr && !gl.getExtension('EXT_color_buffer_float')) throw new Error('render-page: HDR output needs float render targets (EXT_color_buffer_float)');
  if (!(peakNits >= 100 && peakNits <= 10000)) throw new Error(`render-page: peakNits ${peakNits} (100 to 10000)`);

  const conv = createConverter(gl, { width: W, height: H, format, peakNits, dither });
  const target = { framebuffer: conv.drawn.framebuffer, texture: conv.drawn.texture, width: W, height: H, hdr: F.hdr };
  const T = () => performance.now();
  let pending = null; // { i, slot }: a frame already drawn and converted, not yet read back
  let inflight = null; // the previous frame's upload, checked once the next frame is read back
  let audioBytes = null;

  // Draw frame i and convert it into slot `slot`. GPU work only: nothing here waits for it.
  function renderInto(i, slot) {
    if (!(Number.isInteger(i) && i >= 0 && i < frameCount)) throw new Error(`render-page: frame ${i} is outside 0..${frameCount - 1}`);
    if (canvas) {
      draw(i);
      if (canvas.width !== W || canvas.height !== H) throw new Error(`render-page: the canvas is ${canvas.width}x${canvas.height}, the frame ${W}x${H}: size it from renderParams()`);
      // the canvas's own 8-bit values, rows from the bottom (as GL renders), not premultiplied, no colour conversion
      gl.bindTexture(gl.TEXTURE_2D, target.texture);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
      gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL, gl.NONE);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, gl.RGBA, gl.UNSIGNED_BYTE, canvas);
      if (gl.isContextLost()) throw new Error('render-page: WebGL context lost');
      conv.convert(target.texture, slot, i);
      pending = { i, slot };
      return;
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, target.framebuffer);
    gl.viewport(0, 0, W, H);
    const own = draw(i, target);
    if (own != null && !(own instanceof WebGLTexture)) throw new Error('render-page: draw() returns nothing or a WebGLTexture');
    if (gl.isContextLost()) throw new Error('render-page: WebGL context lost'); // a GPU reset would give black frames
    conv.convert(own || target.texture, slot, i);
    pending = { i, slot };
  }

  const glRenderer = (() => {
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    return dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
  })();

  window.__RENDER = {
    version: 1, fps, frameCount, width: W, height: H, format, frameBytes: F.bytes(W, H), glRenderer, info,
    hdr: F.hdr ? { peakNits, dither: !!dither } : null,
    // Frame i's bytes POSTed to `url` (the driver's frame sink). `next`, the frame the driver asks for after this one,
    // is drawn while frame i is read back and sent. Returns the stage timings in milliseconds.
    async frameTo(i, url, next = null) {
      const timing = { frame: i, render: 0, speculate: 0, read: 0, post: 0 };
      let t = T();
      if (!pending || pending.i !== i) renderInto(i, 0);
      timing.render = T() - t;
      const slot = pending.slot;
      t = T();
      if (next != null && next !== i) renderInto(next, 1 - slot); // the GPU starts on it now
      else pending = null;
      timing.speculate = T() - t;
      t = T();
      const bytes = conv.read(slot); // waits for frame i's GPU work
      timing.read = T() - t;
      t = T();
      // The upload is not awaited: the driver's sink resolves the frame when the bytes arrive, and this thread goes back
      // to feeding the GPU. A Blob body (a copy, made now) travels through shared memory; an ArrayBuffer body is
      // streamed through the network service and took twenty times longer.
      const previous = inflight;
      const body = new Blob([bytes]);
      inflight = (async () => {
        for (let attempt = 1; ; attempt++) {
          try {
            const res = await fetch(url, { method: 'POST', body });
            if (!res.ok) throw new Error(`the frame sink answered ${res.status}`);
            return;
          } catch (e) {
            if (attempt === 2) throw e; // a lost connection is retried once before the driver restarts the browser
          }
        }
      })();
      timing.post = T() - t;
      if (previous) await previous; // long done by now (a frame later); a failure surfaces here
      return timing;
    },
    ...(audio ? {
      // The soundtrack, rendered by the page, in pieces (a long WAV is too large for one message to the driver).
      async audioRender(sampleRate = 48000) {
        const b = await audio(sampleRate);
        audioBytes = b instanceof Uint8Array ? b : new Uint8Array(b);
        return { bytes: audioBytes.length, chunks: Math.ceil(audioBytes.length / AUDIO_CHUNK) };
      },
      audioChunk: (k) => toBase64(audioBytes.subarray(k * AUDIO_CHUNK, (k + 1) * AUDIO_CHUNK)),
    } : {}),
  };
  window.__RENDER_READY = true;
  return window.__RENDER;
}
