import type { ReactElement } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import favicon from "../../../public/favicon.svg?raw";
import { BINDERY_STAR, Logo, Wordmark } from "./Logo";

// BND-T-21.1: the family mark from d3cloud.io (DI-REQ-040), "Dog-ear".

afterEach(cleanup);

function svgOf(ui: ReactElement): SVGSVGElement {
  const { container } = render(ui);
  const svg = container.querySelector("svg");
  if (!svg) throw new Error("no svg rendered");
  return svg;
}

/** The mark with what is written on the page removed — what both variants must share. */
function withoutPageContent(svg: SVGSVGElement): string {
  const copy = svg.cloneNode(true) as SVGSVGElement;
  copy.removeAttribute("data-variant");
  copy.querySelector('[data-part="page-content"]')?.remove();
  return copy.outerHTML;
}

describe("Logo", () => {
  it("draws the family ring in the text colour", () => {
    const svg = svgOf(<Logo />);
    expect(svg.getAttribute("viewBox")).toBe("0 0 64 64");
    const ring = svg.querySelector('circle[r="26"]');
    expect(ring?.getAttribute("cx")).toBe("32");
    expect(ring?.getAttribute("cy")).toBe("32");
    expect(ring?.getAttribute("stroke")).toBe("currentColor");
  });

  it("lights exactly one star, in Bindery's colour, where the fold creases", () => {
    const svg = svgOf(<Logo />);
    expect(BINDERY_STAR).toBe("#E8B86D");
    const coloured = [...svg.querySelectorAll("[style]")];
    expect(coloured).toHaveLength(1);
    const star = coloured[0];
    expect(star.getAttribute("cx")).toBe("37");
    expect(star.getAttribute("cy")).toBe("21");
    expect((star as SVGElement).style.fill.toUpperCase()).toBe(BINDERY_STAR);
  });

  it("never paints the ink in a theme or accent colour", () => {
    const svg = svgOf(<Logo variant="mascot" />);
    for (const el of svg.querySelectorAll("[stroke], [fill]")) {
      const stroke = el.getAttribute("stroke");
      const fill = el.getAttribute("fill");
      if (stroke) expect(stroke).toBe("currentColor");
      if (fill && el !== svg) expect(fill).toBe("currentColor");
    }
    expect(svg.innerHTML).not.toMatch(/accent/);
  });

  it("draws the site's two weights: heavy at icon sizes, fine at display sizes", () => {
    const icon = svgOf(<Logo size={28} />);
    expect(icon.querySelector('circle[r="26"]')?.getAttribute("stroke-width")).toBe("3.5");
    expect(icon.querySelector('circle[cx="21"][cy="13"]')?.getAttribute("r")).toBe("3.4");
    expect(icon.querySelector('circle[cx="37"]')?.getAttribute("r")).toBe("5.5");

    const display = svgOf(<Logo size={72} />);
    expect(display.querySelector('circle[r="26"]')?.getAttribute("stroke-width")).toBe("2.2");
    expect(display.querySelector('circle[cx="21"][cy="13"]')?.getAttribute("r")).toBe("2.6");
    expect(display.querySelector('circle[cx="37"]')?.getAttribute("r")).toBe("4.4");
  });

  it("writes two dashed lines of text on the businesslike mark", () => {
    const content = svgOf(<Logo />).querySelector('[data-part="page-content"]');
    const lines = content?.querySelector("path");
    expect(lines?.getAttribute("stroke-dasharray")).toBe("2.5 3.5");
    expect(lines?.getAttribute("opacity")).toBe("0.8");
    expect(lines?.getAttribute("stroke-width")).toBe(String(3.5 * 0.75));
  });

  it("gives the mascot a face in place of the text, and changes nothing else", () => {
    const mark = svgOf(<Logo size={56} />);
    const mascot = svgOf(<Logo size={56} variant="mascot" />);

    const face = mascot.querySelector('[data-part="page-content"]');
    expect(face?.querySelectorAll("circle")).toHaveLength(2);
    expect(face?.querySelector("[stroke-dasharray]")).toBeNull();

    expect(withoutPageContent(mascot)).toBe(withoutPageContent(mark));
  });

  it("is an image named Bindery when it stands alone", () => {
    render(<Logo />);
    expect(screen.getByRole("img", { name: "Bindery" })).toBeTruthy();
  });

  it("is hidden from assistive technology when the name sits beside it", () => {
    const svg = svgOf(<Logo decorative />);
    expect(svg.getAttribute("aria-hidden")).toBe("true");
    expect(svg.getAttribute("role")).toBeNull();
    expect(svg.getAttribute("aria-label")).toBeNull();
  });
});

describe("Wordmark", () => {
  it("does not announce the name twice", () => {
    render(<Wordmark />);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByText("Bindery")).toBeTruthy();
  });
});

describe("favicon.svg", () => {
  it("is the same drawing as the mark", () => {
    const svg = svgOf(<Logo />);
    expect(favicon).toContain('viewBox="0 0 64 64"');
    expect(favicon).toContain('<circle cx="32" cy="32" r="26" />');
    for (const d of [...svg.querySelectorAll("path")].map((p) => p.getAttribute("d"))) {
      expect(favicon).toContain(`d="${d}"`);
    }
    expect(favicon).toContain('class="star" cx="37" cy="21" r="5.5"');
  });

  it("fixes the ink per colour scheme and the star in both", () => {
    expect(favicon).toMatch(/\.ink \{ stroke: #101117; \}/);
    expect(favicon).toMatch(/prefers-color-scheme: dark[\s\S]*\.ink \{ stroke: #f0f2f7; \}/);
    expect(favicon).toContain(`.star { fill: ${BINDERY_STAR}; }`);
  });
});
