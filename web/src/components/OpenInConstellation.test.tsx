import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OpenInConstellation, constellationLink, onAppleDevice } from "./OpenInConstellation";

const IPHONE =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1";
const WINDOWS =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36";

afterEach(() => vi.unstubAllGlobals());

describe("Open in D3 Constellation", () => {
  it("links to the document and page on this server", () => {
    expect(constellationLink("abc-123", 4, "bindery.example")).toBe(
      "d3constellation://bindery.example/bindery/document/abc-123?page=4",
    );
  });

  it("is offered on Apple devices only", () => {
    expect(onAppleDevice(IPHONE)).toBe(true);
    expect(onAppleDevice("Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0)")).toBe(true);
    expect(onAppleDevice(WINDOWS)).toBe(false);
  });

  it("renders nothing elsewhere", () => {
    vi.stubGlobal("navigator", { userAgent: WINDOWS });
    const { container } = render(<OpenInConstellation documentId="abc" page={1} />);
    expect(container.innerHTML).toBe("");
  });

  it("opens the app when tapped", () => {
    vi.stubGlobal("navigator", { userAgent: IPHONE });
    const open = vi.fn();
    render(<OpenInConstellation documentId="abc" page={2} open={open} />);
    fireEvent.click(screen.getByRole("button", { name: "Open in D3 Constellation" }));
    expect(open).toHaveBeenCalledWith(
      `d3constellation://${window.location.host}/bindery/document/abc?page=2`,
    );
  });
});
