(() => {
  let context, master, musicGain, started = false;
  let enabled = localStorage.getItem("wh_audio") !== "off";
  let lastKeyAt = 0;
  const toggle = document.getElementById("soundToggle");
  const updateToggle = () => { if (toggle) { toggle.textContent = enabled ? "🔊" : "🔇"; toggle.setAttribute("aria-label", enabled ? "Выключить звук" : "Включить звук"); toggle.title = enabled ? "Выключить звук" : "Включить звук"; toggle.classList.toggle("muted", !enabled); } };
  function init() { if (!context) { context = new (window.AudioContext || window.webkitAudioContext)(); master = context.createGain(); master.gain.value = .55; master.connect(context.destination); } if (context.state === "suspended") context.resume(); if (enabled && !started) startAmbient(); }
  function tone(frequency, duration, volume, type = "sine", delay = 0) { if (!enabled || !context) return; const oscillator = context.createOscillator(), gain = context.createGain(), now = context.currentTime + delay; oscillator.type = type; oscillator.frequency.setValueAtTime(frequency, now); gain.gain.setValueAtTime(.0001, now); gain.gain.exponentialRampToValueAtTime(volume, now + .012); gain.gain.exponentialRampToValueAtTime(.0001, now + duration); oscillator.connect(gain).connect(master); oscillator.start(now); oscillator.stop(now + duration + .03); }
  function noiseBurst(volume = .06, duration = .04) {
    if (!enabled || !context || !musicGain) return;
    const buffer = context.createBuffer(1, Math.floor(context.sampleRate * duration), context.sampleRate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < data.length; i++) data[i] = (Math.random() * 2 - 1) * (1 - i / data.length);
    const source = context.createBufferSource(), gain = context.createGain();
    source.buffer = buffer; gain.gain.value = volume;
    source.connect(gain).connect(musicGain); source.start();
  }
  function musicNote(frequency, duration, volume, type = "sawtooth", detune = 0) {
    if (!enabled || !context || !musicGain || !frequency) return;
    const oscillator = context.createOscillator(), gain = context.createGain(), now = context.currentTime;
    oscillator.type = type; oscillator.frequency.setValueAtTime(frequency, now); oscillator.detune.value = detune;
    gain.gain.setValueAtTime(.0001, now); gain.gain.linearRampToValueAtTime(volume, now + .012); gain.gain.exponentialRampToValueAtTime(.0001, now + duration);
    oscillator.connect(gain).connect(musicGain); oscillator.start(now); oscillator.stop(now + duration + .03);
  }
  function startAmbient() {
    if (!context || started) return;
    started = true;
    musicGain = context.createGain();
    musicGain.gain.value = .24;
    musicGain.connect(master);
    // Original dark alternative electro-rock composition: four evolving scenes.
    const riffs = [
      [0, 0, 146.83, 0, 130.81, 0, 110, 0, 0, 0, 123.47, 0, 130.81, 0, 98, 0],
      [146.83, 0, 130.81, 0, 155.56, 0, 110, 0, 146.83, 0, 123.47, 0, 98, 0, 110, 0],
      [293.66, 0, 261.63, 220, 311.13, 0, 220, 0, 293.66, 246.94, 261.63, 0, 196, 0, 220, 0],
      [293.66, 261.63, 311.13, 220, 293.66, 246.94, 261.63, 220, 293.66, 0, 329.63, 311.13, 261.63, 0, 220, 0]
    ];
    const basslines = [[36.71, 36.71, 43.65, 32.7], [36.71, 43.65, 46.25, 32.7], [36.71, 46.25, 43.65, 32.7], [36.71, 36.71, 51.91, 32.7]];
    let step = 0;
    const pulse = () => {
      if (context && enabled) {
        const now = context.currentTime;
        const scene = Math.floor(step / 64) % 4, beat = step % 16, bar = Math.floor(step / 4) % 4;
        const note = riffs[scene][beat];
        if (note) musicNote(note, scene < 2 ? .13 : .19, scene < 2 ? .25 : .35, scene === 3 ? "square" : "sawtooth", beat % 2 ? -7 : 7);
        if (beat % 4 === 0) {
          musicNote(basslines[scene][bar], .31, .72, "sawtooth", -5);
          const kick = context.createOscillator(), kickGain = context.createGain();
          kick.type = "sine"; kick.frequency.setValueAtTime(scene < 2 ? 105 : 135, now); kick.frequency.exponentialRampToValueAtTime(38, now + .16);
          kickGain.gain.setValueAtTime(.92, now); kickGain.gain.exponentialRampToValueAtTime(.0001, now + .22);
          kick.connect(kickGain).connect(musicGain); kick.start(now); kick.stop(now + .22);
        }
        if (beat === 8 || beat === 12) noiseBurst(scene < 2 ? .1 : .16, .07);
        else if (beat % 2 === 1) noiseBurst(.055, .025);
        if (scene >= 2 && beat === 0) { musicNote(73.42, .42, .28, "triangle"); musicNote(110, .35, .15, "triangle"); }
        step++;
      }
      setTimeout(pulse, 145);
    };
    pulse();
  }
  const clickSound = () => tone(520, .09, .15, "triangle"), typingSound = () => tone(235, .045, .075, "square"), pasteSound = () => { tone(330, .1, .11, "triangle"); tone(495, .14, .08, "sine", .055); };
  toggle?.addEventListener("click", event => { event.stopPropagation(); enabled = !enabled; localStorage.setItem("wh_audio", enabled ? "on" : "off"); init(); if (musicGain) musicGain.gain.value = enabled ? .24 : 0; updateToggle(); if (enabled) tone(660, .09, .05, "triangle"); });
  document.addEventListener("pointerdown", event => { init(); if (event.target.closest("button") && event.target !== toggle) clickSound(); }, {capture:true});
  document.addEventListener("keydown", event => { if (!event.target.matches("input, textarea, [contenteditable='true']")) return; const now = Date.now(); if (event.key.length === 1 && now - lastKeyAt > 45) { lastKeyAt = now; init(); typingSound(); } }, {capture:true});
  document.addEventListener("paste", event => { if (event.target.matches("input, textarea, [contenteditable='true']")) { init(); pasteSound(); } }, {capture:true});
  // Telegram/WebView may permit this after the tap that opened the Mini App.
  // Other browsers will keep the context suspended until the first real touch.
  window.addEventListener("load", init, {once:true});
  updateToggle();
})();
