// Assembly: tab switching + module wiring. Importing zmq.js registers its
// connect/keyboard handlers (side-effect module by design).
import { S } from './state.js';
import { onLeaveZmq } from './zmq.js';
import { onEnterVision, onLeaveVision } from './vision.js';
import { onEnterVoice, onLeaveVoice } from './voice.js';
import { onEnterRos, onLeaveRos } from './ros.js';
import './leader.js';
import './log.js';
import './health.js';

document.querySelectorAll('.tab').forEach(b => b.onclick = () => {
  const prev = S.page;
  S.page = b.dataset.page;
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x === b));
  document.querySelectorAll('.page').forEach(p =>
    p.classList.toggle('on', p.id === 'page-' + S.page));
  // The bottom log strip echoes the pad/keyboard teleop bus — ZMQ tab only.
  document.getElementById('logpanel').style.display = S.page === 'zmq' ? '' : 'none';
  // Leaving the ZMQ tab must halt the base — a held key would otherwise keep
  // streaming velocity from an unfocused tab.
  if (prev === 'zmq' && S.page !== 'zmq') onLeaveZmq();
  // Vision polling (and the daemon's watch state) is bound to tab visibility:
  // enter promotes to watch + starts polling, leave stands it back down.
  if (prev === 'vision' && S.page !== 'vision') onLeaveVision();
  if (S.page === 'vision' && prev !== 'vision') onEnterVision();
  // Voice: polling only — the conversation window itself lives on the daemon
  // and survives tab switches (no dead-man on purpose).
  if (prev === 'voice' && S.page !== 'voice') onLeaveVoice();
  if (S.page === 'voice' && prev !== 'voice') onEnterVoice();
  // ROS preview: read-only rosbridge subscriptions, socket bound to visibility.
  if (prev === 'ros' && S.page !== 'ros') onLeaveRos();
  if (S.page === 'ros' && prev !== 'ros') onEnterRos();
});
