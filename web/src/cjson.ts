/** WF-CJSON-1 parser and canonical encoder used by the browser client.
 *
 * The browser never adjudicates simulation state. This module only validates
 * content sent to the API and checks the same canonical byte/hash vectors as
 * the Python implementation.
 */
export const MAX_SAFE_INT = 2 ** 53 - 1;
export const MAX_JSON_BYTES = 64 * 1024;

export class CJsonError extends Error {
  readonly code: string;
  readonly path?: string;
  constructor(code: string, message: string, path?: string) {
    super(message);
    this.code = code;
    this.path = path;
  }
}

function checkString(value: string, key = false, path = ""): string {
  if (key && !/^[\x00-\x7f]*$/.test(value)) throw new CJsonError("E_CANONICAL_INPUT", "object keys must contain ASCII only", path);
  if (value.normalize("NFC") !== value) throw new CJsonError("E_CANONICAL_INPUT", "string must be NFC normalized", path);
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdfff) {
      const next = value.charCodeAt(i + 1);
      if (code <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) { i += 1; continue; }
      throw new CJsonError("E_CANONICAL_INPUT", "isolated surrogate is not allowed", path);
    }
  }
  return value;
}

function checkValue(value: unknown, path = ""): void {
  if (value === null || typeof value === "boolean") return;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) throw new CJsonError("E_NUMERIC_TYPE", "floating point values are not allowed", path);
    if (Math.abs(value) > MAX_SAFE_INT) throw new CJsonError("E_INTEGER_RANGE", "integer exceeds safe JSON range", path);
    return;
  }
  if (typeof value === "string") { checkString(value, false, path); return; }
  if (Array.isArray(value)) { value.forEach((v, i) => checkValue(v, `${path}/${i}`)); return; }
  if (typeof value === "object") {
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      checkString(key, true, path ? `${path}/${key}` : `/${key}`);
      checkValue(child, path ? `${path}/${key}` : `/${key}`);
    }
    return;
  }
  throw new CJsonError("E_CANONICAL_INPUT", `unsupported value type ${typeof value}`, path);
}

class Parser {
  private i = 0;
  constructor(private readonly text: string) {}
  parse(): unknown {
    this.ws(); const result = this.value(""); this.ws();
    if (this.i !== this.text.length) throw new CJsonError("E_PARSE", "trailing JSON input");
    checkValue(result); return result;
  }
  private ws(): void { while (this.i < this.text.length && /[\u0009\u000a\u000d\u0020]/.test(this.text[this.i])) this.i += 1; }
  private fail(message: string, path?: string): never { throw new CJsonError("E_PARSE", `${message} at byte ${this.i}`, path); }
  private char(expected: string): void { if (this.text[this.i] !== expected) this.fail(`expected ${expected}`); this.i += 1; }
  private value(path: string): unknown {
    this.ws(); const c = this.text[this.i];
    if (c === "{") return this.object(path); if (c === "[") return this.array(path);
    if (c === '"') return this.string();
    if (this.text.startsWith("true", this.i)) { this.i += 4; return true; }
    if (this.text.startsWith("false", this.i)) { this.i += 5; return false; }
    if (this.text.startsWith("null", this.i)) { this.i += 4; return null; }
    if (c === "-" || /[0-9]/.test(c || "")) return this.number();
    return this.fail("unexpected token", path);
  }
  private string(): string {
    const start = this.i; this.i += 1;
    let escaped = false;
    while (this.i < this.text.length) {
      const code = this.text.charCodeAt(this.i); const c = this.text[this.i];
      if (code < 0x20) this.fail("control character in string");
      if (!escaped && c === '"') { this.i += 1; const raw = this.text.slice(start, this.i); try { return JSON.parse(raw) as string; } catch { return this.fail("invalid string escape"); } }
      if (!escaped && c === "\\") escaped = true; else escaped = false;
      this.i += 1;
    }
    return this.fail("unterminated string");
  }
  private number(): number {
    const start = this.i; if (this.text[this.i] === "-") this.i += 1;
    if (this.text[this.i] === "0") this.i += 1;
    else { if (!/[1-9]/.test(this.text[this.i] || "")) return this.fail("invalid number"); while (/[0-9]/.test(this.text[this.i] || "")) this.i += 1; }
    if (this.text[this.i] === "." || this.text[this.i] === "e" || this.text[this.i] === "E") return this.fail("floating point values are not allowed");
    const token = this.text.slice(start, this.i); const value = Number(token);
    if (!Number.isSafeInteger(value)) throw new CJsonError("E_INTEGER_RANGE", "integer exceeds safe JSON range");
    return value;
  }
  private array(path: string): unknown[] {
    this.char("["); this.ws(); const out: unknown[] = []; if (this.text[this.i] === "]") { this.i += 1; return out; }
    while (true) { out.push(this.value(`${path}/${out.length}`)); this.ws(); if (this.text[this.i] === "]") { this.i += 1; return out; } this.char(","); this.ws(); }
  }
  private object(path: string): Record<string, unknown> {
    this.char("{"); this.ws(); const out: Record<string, unknown> = {}; if (this.text[this.i] === "}") { this.i += 1; return out; }
    while (true) {
      if (this.text[this.i] !== '"') return this.fail("object key must be a string", path);
      const key = checkString(this.string(), true, path ? `${path}/${this.text[this.i]}` : ""); this.ws(); this.char(":"); this.ws();
      if (Object.prototype.hasOwnProperty.call(out, key)) throw new CJsonError("E_DUPLICATE_KEY", `duplicate object key ${key}`);
      out[key] = this.value(path ? `${path}/${key}` : `/${key}`); this.ws();
      if (this.text[this.i] === "}") { this.i += 1; return out; } this.char(","); this.ws();
    }
  }
}

export function parseStrict(source: string | Uint8Array): unknown {
  if (typeof source === "string") {
    for (let i = 0; i < source.length; i += 1) {
      const code = source.charCodeAt(i);
      if (code >= 0xd800 && code <= 0xdbff) {
        const next = source.charCodeAt(i + 1);
        if (!(next >= 0xdc00 && next <= 0xdfff)) throw new CJsonError("E_PARSE", "input is not valid UTF-8 text");
        i += 1;
      } else if (code >= 0xdc00 && code <= 0xdfff) {
        throw new CJsonError("E_PARSE", "input is not valid UTF-8 text");
      }
    }
  }
  const bytes = typeof source === "string" ? new TextEncoder().encode(source) : source;
  if (bytes.byteLength > MAX_JSON_BYTES) throw new CJsonError("E_COMPLEXITY_BUDGET", "JSON input exceeds 64 KiB");
  let text: string; try { text = new TextDecoder("utf-8", { fatal: true }).decode(bytes); } catch { throw new CJsonError("E_PARSE", "input is not valid UTF-8"); }
  return new Parser(text).parse();
}

function encode(value: unknown): string {
  checkValue(value);
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(encode).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0);
  return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${encode(v)}`).join(",")}}`;
}

export function canonicalString(value: unknown): string { return encode(value); }
export function canonicalBytes(value: unknown): Uint8Array { return new TextEncoder().encode(encode(value)); }
export async function digest(domain: string, value: unknown): Promise<string> {
  if (!/^[\x00-\x7f]+$/.test(domain)) throw new CJsonError("E_CANONICAL_INPUT", "digest domain must be non-empty ASCII");
  const prefix = new TextEncoder().encode(`WF:${domain}:v1\0`); const body = canonicalBytes(value); const input = new Uint8Array(prefix.length + body.length); input.set(prefix); input.set(body, prefix.length);
  const hash = await crypto.subtle.digest("SHA-256", input);
  return Array.from(new Uint8Array(hash), (n) => n.toString(16).padStart(2, "0")).join("");
}
export const blueprintHash = (value: unknown) => digest("blueprint", value);

