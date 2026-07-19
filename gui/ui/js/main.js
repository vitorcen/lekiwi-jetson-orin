// Assembly: tab switching + module wiring. Importing zmq.js registers its
// connect/keyboard handlers (side-effect module by design).
import { S } from './state.js';
import { onLeaveZmq } from './zmq.js';
import { onEnterVision, onLeaveVision } from './vision.js';
import './leader.js';
import './log.js';
import './health.js';

document.querySelectorAll('.tab').forEach(b => b.onclick = () => {
  const prev = S.page;
  S.page = b.dataset.page;
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x === b));
  document.querySelectorAll('.page').forEach(p =>
    p.classList.toggle('on', p.id === 'page-' + S.page));
  // Leaving the ZMQ tab must halt the base — a held key would otherwise keep
  // streaming velocity from an unfocused tab.
  if (prev === 'zmq' && S.page !== 'zmq') onLeaveZmq();
  // Vision polling (and the daemon's watch state) is bound to tab visibility:
  // enter promotes to watch + starts polling, leave stands it back down.
  if (prev === 'vision' && S.page !== 'vision') onLeaveVision();
  if (S.page === 'vision' && prev !== 'vision') onEnterVision();
});
