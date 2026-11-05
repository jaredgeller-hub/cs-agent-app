import { useRef, useEffect } from 'react';

export default function ChatInput({ onSend, disabled }) {
  const textareaRef = useRef(null);

  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  function autoResize() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  function submit() {
    const text = textareaRef.current?.value.trim();
    if (!text || disabled) return;
    textareaRef.current.value = '';
    textareaRef.current.style.height = 'auto';
    onSend(text);
  }

  return (
    <div id="input-bar">
      <textarea
        ref={textareaRef}
        id="user-input"
        placeholder="Ask about an account, draft a message, prepare for a call…"
        rows={1}
        onInput={autoResize}
        onKeyDown={handleKeyDown}
      />
      <button id="send-btn" disabled={disabled} onClick={submit}>
        Send
      </button>
    </div>
  );
}
