import android.app.UiAutomation;
import android.graphics.Point;
import android.os.SystemClock;
import android.util.Base64;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.File;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.concurrent.TimeoutException;

/** API35 read-only collector. Two fresh identical trees; no input APIs. */
public class StableUiRead {
    static String hash(byte[] data) throws Exception {
        StringBuilder b=new StringBuilder();for(byte n:MessageDigest.getInstance("SHA-256").digest(data))b.append(String.format("%02x",n&255));return b.toString();
    }
    public static void main(String[] args) {
        int rc=1;Object wrapper=null;Class<?> wc=null;boolean connected=false;File xml=null;
        long started=SystemClock.elapsedRealtime();JSONObject report=new JSONObject();
        JSONArray snapshotDiagnostics=new JSONArray();
        try {
            if(args.length!=1 || !args[0].matches("/sdcard/Download/ai-sandbox-direct-[0-9a-f]{32}\\.xml"))throw new IllegalArgumentException("invalid_owned_path");
            xml=new File(args[0]);report.put("version",1).put("nonce",xml.getName().substring(18,50));
            wc=Class.forName("com.android.uiautomator.core.UiAutomationShellWrapper");wrapper=wc.getConstructor().newInstance();
            wc.getMethod("connect").invoke(wrapper);connected=true;wc.getMethod("setCompressedLayoutHierarchy",boolean.class).invoke(wrapper,false);
            UiAutomation ui=(UiAutomation)wc.getMethod("getUiAutomation").invoke(wrapper);
            try{ui.waitForIdle(100,400);report.put("idle_result","short_idle_reached");}
            catch(TimeoutException e){report.put("idle_result","short_idle_timeout");}
            Class<?> dm=Class.forName("android.hardware.display.DisplayManagerGlobal");Object manager=dm.getMethod("getInstance").invoke(null);
            Display display=(Display)dm.getMethod("getRealDisplay",int.class).invoke(manager,0);Point size=new Point();display.getSize(size);
            Class<?> dumper=Class.forName("com.android.uiautomator.core.AccessibilityNodeInfoDumper");
            byte[][] trees=new byte[2][];int[] windows=new int[2];String[] packages=new String[2];long[] times=new long[2];JSONArray clears=new JSONArray();JSONArray readyPolls=new JSONArray();
            for(int i=0;i<2;i++) {
                if(i==1)Thread.sleep(300);
                times[i]=SystemClock.elapsedRealtime();
                AccessibilityNodeInfo root=null;long readyDeadline=SystemClock.elapsedRealtime()+1500;int polls=0;
                do {
                    boolean cleared=ui.clearCache();root=ui.getRootInActiveWindow();polls++;
                    if(root!=null){clears.put(cleared);break;}
                    if(SystemClock.elapsedRealtime()>=readyDeadline)break;
                    Thread.sleep(100);
                }while(polls<16);
                readyPolls.put(polls);
                if(root==null)throw new IllegalStateException("null_root_after_bounded_readiness");
                try {
                    windows[i]=root.getWindowId();packages[i]=String.valueOf(root.getPackageName());
                    dumper.getMethod("dumpWindowToFile",AccessibilityNodeInfo.class,File.class,int.class,int.class,int.class)
                        .invoke(null,root,xml,display.getRotation(),size.x,size.y);
                    if(!xml.isFile() || xml.length()==0 || xml.length()>2097152)throw new IllegalStateException("invalid_xml_size");
                    trees[i]=Files.readAllBytes(xml.toPath());
                    snapshotDiagnostics.put(new JSONObject().put("index",i+1).put("window_id",windows[i])
                        .put("package",packages[i]).put("sha256",hash(trees[i])).put("xml_bytes",trees[i].length)
                        .put("taken_ms",times[i]-started).put("cache_cleared",clears.getBoolean(i)));
                } finally{root.recycle();}
            }
            if(windows[0]<0 || windows[0]!=windows[1] || !packages[0].equals(packages[1]) || packages[0].equals("null"))throw new IllegalStateException("window_changed");
            if(!Arrays.equals(trees[0],trees[1]))throw new IllegalStateException("unstable_tree");
            report.put("ok",true).put("stable",true).put("snapshot_count",2).put("snapshot_gap_ms",times[1]-times[0])
                .put("cache_clear",clears).put("root_ready_polls",readyPolls).put("window_id",windows[0]).put("package",packages[0])
                .put("sha256_first",hash(trees[0])).put("sha256_second",hash(trees[1]))
                .put("xml_b64",Base64.encodeToString(trees[1],Base64.NO_WRAP));rc=0;
        }catch(Throwable e){
            try{report.put("ok",false).put("error",String.valueOf(e)).put("cause",String.valueOf(e.getCause()));
                if(snapshotDiagnostics.length()>0)report.put("snapshot_diagnostics",snapshotDiagnostics);
            }catch(Exception ignored){}
        }finally{
            if(connected)try{wc.getMethod("disconnect").invoke(wrapper);}catch(Exception e){rc=1;}
            if(xml!=null && xml.exists() && !xml.delete())rc=1;
            try{report.put("elapsed_ms",SystemClock.elapsedRealtime()-started);System.out.println(report.toString());}catch(Exception e){rc=1;}
        }
        System.exit(rc);
    }
}
