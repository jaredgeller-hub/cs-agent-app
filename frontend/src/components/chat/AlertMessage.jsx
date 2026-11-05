import ReactMarkdown from 'react-markdown';
import rehypeSanitize from 'rehype-sanitize';

export default function AlertMessage({ content }) {
  return (
    <div className="msg-row msg-agent">
      <div className="bubble" style={{ borderLeftColor: 'var(--accent-warm)' }}>
        <ReactMarkdown rehypePlugins={[rehypeSanitize]}>{content}</ReactMarkdown>
      </div>
    </div>
  );
}
