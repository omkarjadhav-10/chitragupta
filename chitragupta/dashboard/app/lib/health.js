// Current problems shared by the navigation and Health. Optional integrations being off are not failures.
export function healthIssues(s) {
  if (!s) return [];
  const out = [];
  const add = (title, detail, href = '#/health') => out.push({ title, detail, href });
  if (s.paused) add('Capture is paused', 'Resume with /cg:pause off to record new work.');
  else if (s.hooks && s.hooks.minutes_ago == null) add('No capture events yet', 'Start a Claude Code session in a work repo. Check Setup if events do not appear.', '#/setup');
  const tick = s.tick || {};
  if (tick.minutes_ago == null || tick.minutes_ago > 15) add('Background runs need a look',
    tick.minutes_ago == null ? 'No background run has been recorded. Check the background job below.' : 'The last run was more than 15 minutes ago. Check the background job below.');
  if ((s.push || {}).error) add('Daily push failed', s.push.error);
  if ((s.slack || {}).send_warning) add('Slack delivery needs a look', s.slack.send_warning);
  const mc = s.mcp || {};
  const live = (mc.live && (mc.live.servers || mc.live.rows)) || [];
  (mc.configured || []).filter((c) => c.enabled).forEach((c) => {
    const hit = live.find((x) => (x.name || x.server) === c.server) || (mc.servers || []).find((x) => x.server === c.server);
    if (!hit || hit.status !== 'connected') add((c.role === 'database' ? 'Database MCP' : 'Jira MCP') + ' needs a look',
      'Connection: ' + (hit ? hit.status || 'unknown' : 'unknown') + '. Review MCP status below; your worklog is still recorded.');
  });
  if (s.safety && !s.safety.enforced) add('Safety checks need a look', (s.safety.problems || []).join('; ') || 'Run /cg:doctor for diagnostics.');
  return out;
}
