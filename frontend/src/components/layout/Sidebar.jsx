import { useState } from 'react';

function healthDotClass(score) {
  if (score == null) return 'health-neutral';
  if (score >= 85) return 'health-ok';
  if (score >= 70) return 'health-watch';
  return 'health-risk';
}

export default function Sidebar({ accounts, onAccountClick }) {
  const [collapsed, setCollapsed] = useState(
    () => sessionStorage.getItem('cs_agent_sidebar_collapsed') === '1'
  );

  function toggle() {
    const next = !collapsed;
    setCollapsed(next);
    sessionStorage.setItem('cs_agent_sidebar_collapsed', next ? '1' : '0');
  }

  return (
    <aside id="right-sidebar" className={collapsed ? 'collapsed' : ''}>
      <div id="right-toggle">
        <button onClick={toggle} title="Toggle accounts panel">
          {collapsed ? '◀' : '▶'}
        </button>
      </div>
      <div id="right-sidebar-inner">
        <h3>Accounts</h3>
        <div id="account-list">
          {accounts.length === 0 ? (
            <p style={{ padding: '8px 16px', fontSize: 12, color: 'var(--text-muted)', fontStyle: 'italic' }}>
              No accounts found
            </p>
          ) : (
            accounts.map(acct => (
              <div
                key={acct.name}
                className="account-row"
                onClick={() => onAccountClick && onAccountClick(acct.name)}
              >
                <div className={`health-dot ${healthDotClass(acct.health_score)}`} />
                <div className="acct-name">{acct.name}</div>
                {acct.renewal_days != null && acct.renewal_days <= 90 && (
                  <div className="renewal-badge">{acct.renewal_days}d</div>
                )}
              </div>
            ))
          )}
        </div>
      </div>
    </aside>
  );
}
