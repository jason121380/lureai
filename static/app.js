// chat 與 admin 共用的頁面行為（跟 app.css 一樣是兩邊共用的那一份）。
(() => {
  // PWA 要像 App：關掉縮放。
  // viewport 的 user-scalable=no 在 iOS Safari 的「瀏覽器分頁」裡會被忽略
  // （Apple 為了無障礙刻意不理），加到主畫面之後才生效——所以捏合手勢還要
  // 自己擋一次，否則在 Safari 裡開仍然會被兩指放大。
  ["gesturestart", "gesturechange", "gestureend"].forEach((name) => {
    document.addEventListener(name, (event) => event.preventDefault(), { passive: false });
  });
})();
