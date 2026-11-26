const { chromium } = require('playwright');
const path = require('path');
const URL = process.env.LK_URL, POLICY = process.env.POLICY || 'relay';
(async () => {
  const browser = await chromium.launch({ args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--no-sandbox'] });
  const ctx = await browser.newContext({ permissions: ['microphone'] });
  const page = await ctx.newPage();
  await page.addInitScript(() => { window.__pcs = []; window.__added = []; const AI = RTCPeerConnection.prototype.addIceCandidate; RTCPeerConnection.prototype.addIceCandidate = function (c) { try { window.__added.push(c && (c.candidate || JSON.stringify(c))); } catch (e) {} const pr = AI.apply(this, arguments); window.__addRes = window.__addRes || []; Promise.resolve(pr).then(() => window.__addRes.push('ok'), e => window.__addRes.push('REJECTED: ' + (e && e.message))); return pr; }; const O = window.RTCPeerConnection; window.RTCPeerConnection = function (...a) { const pc = new O(...a); window.__pcs.push(pc); window.__iceErr = window.__iceErr || []; pc.addEventListener('icecandidateerror', ev => window.__iceErr.push([ev.url, ev.errorCode, ev.errorText].join(' '))); return pc; }; window.RTCPeerConnection.prototype = O.prototype; });
  await page.goto(process.env.PAGE_URL, { waitUntil: 'domcontentloaded' });  // any https page gives a secure context for getUserMedia
  await page.addScriptTag({ path: path.join(__dirname, 'node_modules/livekit-client/dist/livekit-client.umd.js') });
  const out = await page.evaluate(async ({ url, tokA, tokB, policy }) => {
    const L = window.LivekitClient, sleep = ms => new Promise(r => setTimeout(r, ms)); const res = {};
    const collect = async () => { const out = [];
      for (const pc of window.__pcs) {
        const stats = await pc.getStats(); const loc = {}, rem = {}, pairs = [], errs = [];
        stats.forEach(s => { if (s.type === 'local-candidate') loc[s.id] = s.candidateType + ':' + (s.relayProtocol || s.protocol || '') + (s.address ? '@' + s.address + ':' + s.port : ''); if (s.type === 'remote-candidate') rem[s.id] = s.candidateType + ':' + s.protocol + '@' + s.address + ':' + s.port; });
        stats.forEach(s => { if (s.type === 'candidate-pair') pairs.push({ state: s.state, nominated: s.nominated, local: loc[s.localCandidateId], remote: rem[s.remoteCandidateId], reqSent: s.requestsSent, respRecv: s.responsesReceived, bytesSent: s.bytesSent, bytesReceived: s.bytesReceived }); });
        const sdp = (pc.remoteDescription && pc.remoteDescription.sdp || '').split('\n').filter(l => l.startsWith('a=candidate') || l.startsWith('a=ice-lite')).map(l => l.trim()); out.push({ addResults: (window.__addRes || []).slice(0, 14), iceErrors: (window.__iceErr || []).slice(0, 6), remoteSdpCandidates: sdp, addedRemote: window.__added.slice(0, 12), connectionState: pc.connectionState, iceState: pc.iceConnectionState, localCandidates: Object.values(loc), remoteCandidates: Object.values(rem), pairs: pairs.slice(0, 6) });
      } return out; };
    const opts = { rtcConfig: { iceTransportPolicy: policy } };
    const pub = new L.Room(), sub = new L.Room();
    window.__lastSnap = null;
    const iv = setInterval(async () => { try { const c = await collect(); if (c.some(x => x.localCandidates.length)) { window.__lastSnap = c; const score = c.reduce((a, x) => a + x.remoteCandidates.length * 10 + x.pairs.length, 0); if (!window.__best || score >= window.__bestScore) { window.__best = c; window.__bestScore = score; } } } catch (e) {} }, 400);
    const t0 = Date.now(); const stamp = () => ((Date.now() - t0) / 1000).toFixed(1) + 's';
    try { await sub.connect(url, tokB, opts); res.subConnect = 'ok ' + stamp(); } catch (e) { res.subConnect = 'FAILED ' + e.message; }
    try { await pub.connect(url, tokA, opts); res.pubConnect = 'ok ' + stamp(); } catch (e) { res.pubConnect = 'FAILED ' + e.message; clearInterval(iv); res.pcs = window.__best || window.__lastSnap || await collect(); res.bestScore = window.__bestScore; return res; }
    try { await pub.localParticipant.setMicrophoneEnabled(true); res.micPublished = true; } catch (e) { res.micPublished = 'FAILED ' + e.message; }
    await sleep(9000);
    // sender stats
    try { const p = pub.localParticipant.getTrackPublication('microphone'); const st = await p.track.getSenderStats(); res.pubSender = { bytesSent: st && st.bytesSent, packetsSent: st && st.packetsSent }; } catch (e) { res.pubSender = 'n/a ' + e.message; }
    // receiver stats on the subscriber
    try { const rp = [...sub.remoteParticipants.values()][0]; const pubn = rp && [...rp.trackPublications.values()][0]; const tr = pubn && pubn.track; const st = tr && await tr.getReceiverStats(); res.subReceiver = rp ? { remoteIdentity: rp.identity, subscribed: !!tr, packetsReceived: st && st.packetsReceived, bytesReceived: st && st.bytesReceived } : 'no remote participant seen'; } catch (e) { res.subReceiver = 'n/a ' + e.message; }
    clearInterval(iv); res.pcs = await collect();
    await pub.disconnect(); await sub.disconnect();
    return res;
  }, { url: URL, tokA: process.env.TOKEN_A, tokB: process.env.TOKEN_B, policy: POLICY });
  console.log(JSON.stringify(out, null, 1));
  await browser.close();
})().catch(e => { console.error('HARNESS ERROR', e.message); process.exit(1); });
