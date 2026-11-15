package tw.magic.otccount;

import android.os.Bundle;
import android.webkit.WebView;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // 不跟著手機系統字體放大，字體大小以 App 內「設定 → 字體大小」為準
        try {
            WebView wv = getBridge().getWebView();
            if (wv != null) wv.getSettings().setTextZoom(100);
        } catch (Exception ignored) {}
    }
}
