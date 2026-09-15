import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { TooltipProvider } from "@d3cloud/ui";

import App from "./App";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* Shares tooltip delays app-wide: once one has opened, moving along a
        toolbar shows the next immediately instead of waiting again. A Tooltip
        works without it; this is what makes a row of them feel responsive. */}
    <TooltipProvider delayDuration={400} skipDelayDuration={300}>
      <App />
    </TooltipProvider>
  </StrictMode>,
);
