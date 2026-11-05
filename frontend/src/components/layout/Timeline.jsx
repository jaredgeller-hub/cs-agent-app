function formatEventTime(evt) {
  if (evt.time_iso) {
    return new Date(evt.time_iso).toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    });
  }
  return evt.time || '';
}

function EventRow({ evt, isLast, upcoming }) {
  return (
    <div className={'timeline-item' + (evt.past ? ' past' : '')}>
      <div className="timeline-track">
        <div className={upcoming ? 'timeline-dot upcoming' : 'timeline-dot'} />
        {!isLast && <div className="timeline-connector" />}
      </div>
      <div className="timeline-content">
        <span className="time">{formatEventTime(evt)}</span>
        <span className="label">{evt.label || ''}</span>
      </div>
    </div>
  );
}

function SectionHeader({ children }) {
  return (
    <h3 style={{
      fontSize: 11,
      fontWeight: 600,
      textTransform: 'uppercase',
      letterSpacing: '0.08em',
      color: 'var(--text-muted)',
      padding: '16px 16px 8px',
      margin: 0,
    }}>
      {children}
    </h3>
  );
}

export default function Timeline({ events }) {
  if (!events || events.length === 0) {
    return (
      <>
        <SectionHeader>Today</SectionHeader>
        <p style={{ padding: '8px 16px', fontSize: 13, color: 'var(--text-muted)', fontStyle: 'italic' }}>
          No events today
        </p>
      </>
    );
  }

  const now = new Date();

  const pastEvents = events
    .filter(e => {
      if (e.time_iso) return new Date(e.time_iso) <= now;
      return e.past === true;
    })
    .sort((a, b) => {
      if (a.time_iso && b.time_iso) return new Date(a.time_iso) - new Date(b.time_iso);
      return 0;
    });

  const upcomingEvents = events
    .filter(e => {
      if (e.time_iso) return new Date(e.time_iso) > now;
      return e.past !== true;
    })
    .sort((a, b) => {
      if (a.time_iso && b.time_iso) return new Date(a.time_iso) - new Date(b.time_iso);
      return 0;
    });

  return (
    <>
      {pastEvents.length > 0 && (
        <>
          <SectionHeader>Today</SectionHeader>
          <div id="timeline-list">
            {pastEvents.map((evt, i) => (
              <EventRow
                key={'past-' + i}
                evt={evt}
                isLast={i === pastEvents.length - 1 && upcomingEvents.length === 0}
                upcoming={false}
              />
            ))}
          </div>
        </>
      )}

      {upcomingEvents.length > 0 && (
        <>
          <SectionHeader>Upcoming</SectionHeader>
          <div id="timeline-list">
            {upcomingEvents.map((evt, i) => (
              <EventRow
                key={'upcoming-' + i}
                evt={evt}
                isLast={i === upcomingEvents.length - 1}
                upcoming={true}
              />
            ))}
          </div>
        </>
      )}
    </>
  );
}
