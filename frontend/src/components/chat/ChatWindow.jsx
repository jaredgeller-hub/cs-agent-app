import { useEffect, useRef } from 'react';
import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeSanitize from 'rehype-sanitize';
import DraftCard from './DraftCard';
import PostCallDraftCard from './PostCallDraftCard';
import ChipRow from './ChipRow';
import api from '../../lib/api';

function AgentBubble({ content, isError }) {
  const [flagged, setFlagged] = useState(false);

  async function flag() {
    if (flagged) return;
    setFlagged(true);
    api.post('/app/feedback', { message_text: content }).catch(() => {});
  }

  return (
    <div className="msg-row msg-agent">
      <div className={`bubble${flagged ? ' flagged' : ''}`} style={isError ? { borderLeftColor: '#B85C38', color: 'var(--text-muted)' } : {}}>
        <div className="md">
          <ReactMarkdown rehypePlugins={[rehypeSanitize]}>{content}</ReactMarkdown>
        </div>
      </div>
      <button
        className={`flag-btn${flagged ? ' flagged' : ''}`}
        onClick={flag}
        title="Flag this response as feedback"
      >
        {flagged ? '✓ Flagged' : '⚑ Flag'}
      </button>
    </div>
  );
}

function TypingIndicator() {
  return (
    <div className="msg-row msg-agent" id="typing-row" style={{ display: 'flex' }}>
      <div className="bubble" style={{ padding: 0, border: '1px solid var(--border)', borderLeft: '3px solid var(--accent)' }}>
        <div className="typing-dots">
          <span /><span /><span />
        </div>
      </div>
    </div>
  );
}

export default function ChatWindow({ messages, loading, onChipClick }) {
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  return (
    <div id="messages">
      {messages.map(msg => {
        if (msg.role === 'user') {
          return (
            <div key={msg.id} className="msg-row msg-user">
              <div className="bubble">{msg.content}</div>
            </div>
          );
        }
        if (msg.role === 'draft') {
          return <DraftCard key={msg.id} label={msg.label} content={msg.content} actionType={msg.actionType} actionPayload={msg.actionPayload} accountName={msg.accountName} />;
        }
        if (msg.role === 'chips') {
          return <ChipRow key={msg.id} chips={msg.chips || []} onChipClick={onChipClick} />;
        }
        if (msg.role === 'postcall_draft') {
          const d = msg.content || {};
          return (
            <PostCallDraftCard
              key={msg.id}
              recap={d.recap}
              draftText={d.draft_text}
              accountName={d.account_name}
              accountCrmId={d.account_crm_id}
              crmType={d.crm_type}
              draftedEventId={d.drafted_event_id}
            />
          );
        }
        return <AgentBubble key={msg.id} content={msg.content} isError={msg.isError} />;
      })}
      {loading && <TypingIndicator />}
      <div ref={bottomRef} />
    </div>
  );
}
