(() => {
  const canvas = document.getElementById("matrixRain");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const glyphs = "01ABCDEFGHIJKLMNOPQRSTUVWXYZ$↗↘";
  let columns = [], width = 0, height = 0, frame;
  const resize = () => {
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    width = window.innerWidth; height = window.innerHeight;
    canvas.width = Math.floor(width * ratio); canvas.height = Math.floor(height * ratio);
    canvas.style.width = `${width}px`; canvas.style.height = `${height}px`;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    const cell = 14, band = Math.max(72, Math.min(width * .27, 190));
    columns = [];
    for (let x = 6; x < band; x += cell) columns.push({x, y: Math.random() * -height, speed: .55 + Math.random() * 1.15});
    for (let x = width - band; x < width; x += cell) columns.push({x, y: Math.random() * -height, speed: .55 + Math.random() * 1.15});
  };
  const draw = () => {
    ctx.clearRect(0, 0, width, height);
    ctx.font = "14px ui-monospace, monospace";
    columns.forEach(column => {
      ctx.fillStyle = "rgba(88, 255, 181, .88)";
      ctx.fillText(glyphs[Math.floor(Math.random() * glyphs.length)], column.x, column.y);
      ctx.fillStyle = "rgba(17, 210, 128, .28)";
      for (let tail = 1; tail < 13; tail++) ctx.fillText(glyphs[Math.floor(Math.random() * glyphs.length)], column.x, column.y - tail * 14);
      column.y += column.speed * 2.8;
      if (column.y > height + 170) { column.y = -30; column.speed = .55 + Math.random() * 1.15; }
    });
    frame = requestAnimationFrame(draw);
  };
  document.addEventListener("visibilitychange", () => { if (document.hidden) cancelAnimationFrame(frame); else draw(); });
  window.addEventListener("resize", resize);
  resize(); draw();
})();
