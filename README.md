# 股市智多星

台股報價 Android App（Capacitor）。

## 更新方式
- **只改介面／功能（www/app.html）**：改完把 `www/ota.json` 的 `v` 和 `app.html` 裡的 `UI_V` 都加 1，推到 main。
  手機打開 App 會自動下載新介面，不用重新安裝。
- **改到原生部分（圖示、外掛、權限）**：把 `android/app/build.gradle` 的 `versionCode` 加 1，推到 main。
  GitHub Actions 會編譯新 APK，App 內會提示下載安裝。
