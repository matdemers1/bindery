import { fireEvent, render, screen } from "@testing-library/react";
import { Modal as LibraryModal } from "@d3cloud/ui";
import { describe, expect, it, vi } from "vitest";

import Modal from "./Modal";

describe("Modal with a portalled dialog opened from inside it", () => {
  it("leaves Escape pressed in the inner dialog to the inner dialog", () => {
    const onClose = vi.fn();
    render(
      <Modal label="Lightbox" onClose={onClose}>
        <button>In the lightbox</button>
        <LibraryModal open title="Move this to the vault?" onOpenChange={() => {}}>
          <button>Inside the confirmation</button>
        </LibraryModal>
      </Modal>,
    );
    const inner = screen.getByRole("button", { name: "Inside the confirmation" });
    fireEvent.keyDown(inner, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("still closes on Escape pressed inside its own panel", () => {
    const onClose = vi.fn();
    render(
      <Modal label="Lightbox" onClose={onClose}>
        <button>In the lightbox</button>
      </Modal>,
    );
    fireEvent.keyDown(screen.getByRole("button", { name: "In the lightbox" }), { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });
});
