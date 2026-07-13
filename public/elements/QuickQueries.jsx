import { Button } from "@/components/ui/button";

// Fixed sidebar list of one-click "quick query" buttons. Rendered once into the
// element sidebar on chat start (see app.py `_show_quick_queries`) instead of
// being re-attached to every assistant message, so the buttons stay put and
// don't clutter the conversation flow.
//
// `props.queries` is the config-driven `settings.action_buttons` list. Clicking a
// button fires the same `quick_query` action callback the per-message buttons used
// to, so no backend handler logic changes.
export default function QuickQueries() {
  const queries = props.queries || [];
  return (
    <div className="flex flex-col gap-2">
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
