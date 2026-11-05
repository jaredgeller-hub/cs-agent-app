import { useState } from 'react';

// First-run helper chips. Rendered once, immediately after the welcome-wrapped
// morning digest. A single tap hides the entire row (so the chat doesn't carry
// a stale set of suggestions through the rest of the day) and routes the chip
// text through the same chat path as the sidebar "Tell me about <account>"
// click. No LLM call, no account names. Backend supplies the three strings;
// the frontend renders them verbatim.
export default function ChipRow({ chips, onChipClick }) {
  const [used, setUsed] = useState(false);
  if (used || !chips || !chips.length) return null;
  return (
    <div className="msg-row msg-agent">
      <div className="chip-row">
        {chips.map((c, i) => (
          <button
            key={i}
            className="chip"
            onClick={() => { setUsed(true); onChipClick?.(c); }}
          >
            {c}
          </button>
        ))}
      </div>
    </div>
  );
}
