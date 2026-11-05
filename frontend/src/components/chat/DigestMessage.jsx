import ReactMarkdown from 'react-markdown';
import rehypeSanitize from 'rehype-sanitize';

export default function DigestMessage({ content, isError }) {
  return (
    <div className="msg-row msg-agent">
      <div
        className="bubble"
        style={isError ? { borderLeftColor: '#B85C38', color: 'var(--text-muted)' } : {}}
      >
        <ReactMarkdown rehypePlugins={[rehypeSanitize]}>{content}</ReactMarkdown>
      </div>
    </div>
  );
}
