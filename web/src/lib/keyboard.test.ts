import { describe, expect, it } from "vitest";

import { isInteractiveTarget, isTypingTarget } from "./keyboard";

function el(html: string): HTMLElement {
  const host = document.createElement("div");
  host.innerHTML = html;
  return host.firstElementChild as HTMLElement;
}

describe("isTypingTarget", () => {
  it.each([
    ["<input />"],
    ["<textarea></textarea>"],
    ["<select></select>"],
    ['<div contenteditable="true"></div>'],
  ])("claims %s, so a shortcut does not eat what is being typed", (html) => {
    expect(isTypingTarget(el(html))).toBe(true);
  });

  it("does not claim a plain region", () => {
    expect(isTypingTarget(el("<section></section>"))).toBe(false);
  });

  it("does not claim contenteditable='false'", () => {
    expect(isTypingTarget(el('<div contenteditable="false"></div>'))).toBe(false);
  });
});

describe("isInteractiveTarget", () => {
  it.each([
    ["<button>Accept</button>"],
    ['<a href="/x">go</a>'],
    ['<div role="button"></div>'],
    ['<div role="tab"></div>'],
    ['<div role="option"></div>'],
  ])("claims %s, so the control keeps its own key", (html) => {
    expect(isInteractiveTarget(el(html))).toBe(true);
  });

  it("claims a child of a control, because that is what focus lands on", () => {
    const button = el("<button><span>Accept</span></button>");
    expect(isInteractiveTarget(button.querySelector("span"))).toBe(true);
  });

  it("does not claim the page body, where the shortcut is meant to work", () => {
    expect(isInteractiveTarget(document.body)).toBe(false);
  });

  it("is false for a null target rather than throwing", () => {
    expect(isInteractiveTarget(null)).toBe(false);
    expect(isTypingTarget(null)).toBe(false);
  });
});
