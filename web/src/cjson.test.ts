import { describe, expect, it } from "vitest";
import vectors from "../../test_vectors/canonical.json";
import { blueprintHash, canonicalBytes, parseStrict } from "./cjson";

describe("WF-CJSON-1 golden vectors", () => {
  for (const vector of vectors as Array<{ name: string; input: unknown; canonical_utf8_hex: string; blueprint_domain_sha256: string }>) {
    it(vector.name, async () => {
      expect(Array.from(canonicalBytes(vector.input), n => n.toString(16).padStart(2, "0")).join("")).toBe(vector.canonical_utf8_hex);
      expect(await blueprintHash(vector.input)).toBe(vector.blueprint_domain_sha256);
      expect(parseStrict(canonicalBytes(vector.input))).toEqual(vector.input);
    });
  }
  it("rejects duplicate keys, floats and non-ASCII keys", () => {
    expect(() => parseStrict('{"a":1,"a":2}')).toThrowError(/duplicate/);
    expect(() => parseStrict('{"a":1.0}')).toThrowError(/floating/);
    expect(() => parseStrict('{"河":1}')).toThrowError(/ASCII/);
  });
});

