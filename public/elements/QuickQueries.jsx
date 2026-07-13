import { Button } from "@/components/ui/button";

// Fixed sidebar list of one-click "quick query" buttons. Rendered once into the
// element sidebar on chat start (see app.py `_show_quick_queries`) instead of
// being re-attached to every assistant message, so the buttons stay put and
// don't clutter the conversation flow.
//
// `props.queries` is the config-driven `settings.action_buttons` list. Clicking a
// button fires the same `quick_query` action callback the per-message buttons used
// to, so no backend handler logic changes.
//
// Collapse/reopen: Chainlit's built-in sidebar close arrow doesn't collapse — its
// onClick clears the sidebar state, unmounting the panel with NO way to reopen it.
// So we hide that arrow and drive a non-destructive show/hide from our own floating
// toggle button. Everything below anchors on Chainlit's stable ids
// (#side-view-title / #side-view-content) and react-resizable-panels' [data-panel]
// attributes; if a Chainlit upgrade moves those, the toggle simply doesn't install
// (the buttons keep working) rather than breaking the app.

const TOGGLE_ID = "qq-sidebar-toggle";
const STYLE_ID = "qq-sidebar-style";

function ensureStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  // Hide the built-in close arrow (it destroys the panel) and give ourselves a
  // class that collapses the whole sidebar column, reclaiming the space for chat.
  style.textContent =
    "#side-view-title button{display:none!important}" +
    ".qq-collapsed{display:none!important}";
  document.head.appendChild(style);
}

function resizeHandleFor(panel) {
  // The PanelResizeHandle is rendered just before the sidebar Panel.
  const prev = panel.previousElementSibling;
  if (prev && (prev.getAttribute("role") === "separator" || prev.hasAttribute("data-resize-handle"))) {
    return prev;
  }
  return null;
}

function setCollapsed(panel, collapsed) {
  panel.classList.toggle("qq-collapsed", collapsed);
  const handle = resizeHandleFor(panel);
  if (handle) handle.style.display = collapsed ? "none" : "";
  const btn = document.getElementById(TOGGLE_ID);
  if (btn) {
    btn.textContent = collapsed ? "🧰 Quick queries" : "✕ Hide quick queries";
    btn.setAttribute("aria-expanded", String(!collapsed));
  }
}

function installSidebarToggle(node) {
  const panel = node.closest("[data-panel]");
  if (!panel) return; // layout changed under us — leave the app untouched
  ensureStyle();
  if (!document.getElementById(TOGGLE_ID)) {
    const btn = document.createElement("button");
    btn.id = TOGGLE_ID;
    btn.type = "button";
    btn.style.cssText = [
      "position:fixed",
      "left:1rem",
      "bottom:1rem",
      "z-index:9999",
      "font:500 0.85rem system-ui,-apple-system,Segoe UI,Roboto,sans-serif",
      "padding:0.4rem 0.9rem",
      "border-radius:999px",
      "cursor:pointer",
      "color:inherit",
      "background:rgba(128,128,128,0.12)",
      "border:1px solid rgba(128,128,128,0.28)",
      "backdrop-filter:blur(4px)",
    ].join(";");
    btn.addEventListener("click", () => {
      setCollapsed(panel, !panel.classList.contains("qq-collapsed"));
    });
    document.body.appendChild(btn);
  }
  setCollapsed(panel, false); // panel starts expanded
}

function teardownSidebarToggle() {
  const btn = document.getElementById(TOGGLE_ID);
  if (btn) btn.remove();
}

// Ref callback: runs with the DOM node on mount and with null on unmount (e.g. a
// new chat). Defined at module scope so its identity is stable across re-renders.
function mountToggle(node) {
  if (node) installSidebarToggle(node);
  else teardownSidebarToggle();
}

export default function QuickQueries() {
  const queries = props.queries || [];
  return (
    <div ref={mountToggle} className="flex flex-col gap-2">
      {queries.map((query, i) => (
        <Button
          key={i}
          variant="outline"
          className="justify-start h-auto whitespace-normal text-left"
          onClick={() => callAction({ name: "quick_query", payload: { query } })}
        >
          {query}
        </Button>
      ))}
    </div>
  );
}
