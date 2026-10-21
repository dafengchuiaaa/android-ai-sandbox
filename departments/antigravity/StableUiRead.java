// FILE: candidate/StableUiRead.java
import android.app.UiAutomation;
import android.graphics.Point;
import android.os.SystemClock;
import android.util.Base64;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.File;
import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.concurrent.TimeoutException;

/** API35 read-only collector. Two fresh identical trees; no input APIs. */
public class StableUiRead {
    static final int MAX_STDOUT_CHARS = 3800;
    static final int MAX_PKG_Diagnostic_LEN = 128;
    static final int MAX_FAILURE_XML_BYTES = 131072;

    // Diagnostic bytes only. This never supplies an observation or changes rc.
    static byte[] failureSnapshotBytes(String nonce, byte[][] trees, int[] windows,
            String[] packages, long[] times, boolean[] clearFlags, long started) throws Exception {
        if (!nonce.matches("[0-9a-f]{32}") || trees[0] == null || trees[1] == null)
            throw new IllegalArgumentException("failure_snapshot_identity");
        if (trees[0].length == 0 || trees[1].length == 0 ||
                (long)trees[0].length + trees[1].length > MAX_FAILURE_XML_BYTES)
            throw new IllegalArgumentException("failure_snapshot_size");
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        DataOutputStream out = new DataOutputStream(bytes);
        out.write("AISF1".getBytes(StandardCharsets.US_ASCII));
        out.write(nonce.getBytes(StandardCharsets.US_ASCII));
        out.writeByte(2);
        for (int i = 0; i < 2; i++) {
            if (packages[i] == null || packages[i].length() > MAX_PKG_Diagnostic_LEN ||
                    windows[i] < 0 || times[i] < started)
                throw new IllegalArgumentException("failure_snapshot_metadata");
            byte[] pkg = packages[i].getBytes(StandardCharsets.UTF_8);
            out.writeInt(windows[i]);
            out.writeLong(times[i] - started);
            out.writeBoolean(clearFlags[i]);
            out.writeShort(pkg.length);
            out.writeInt(trees[i].length);
            out.write(pkg);
            out.write(trees[i]);
        }
        out.flush();
        return bytes.toByteArray();
    }

    static String hash(byte[] data) throws Exception {
        StringBuilder b=new StringBuilder();for(byte n:MessageDigest.getInstance("SHA-256").digest(data))b.append(String.format("%02x",n&255));return b.toString();
    }

    static String capStr(String s, int maxLen) {
        if (s == null) return "null";
        return s.length() <= maxLen ? s : s.substring(0, maxLen) + "...";
    }

    static JSONArray buildSnapshotDiagnostics(
            int capturedCount,
            byte[][] trees,
            int[] windows,
            String[] packages,
            long[] times,
            boolean[] clearFlags,
            long started) {
        JSONArray arr = new JSONArray();
        for (int i = 0; i < capturedCount && i < 2; i++) {
            if (trees[i] == null) continue;
            try {
                JSONObject item = new JSONObject();
                item.put("index", i + 1)
                    .put("window_id", windows[i])
                    .put("package", capStr(packages[i], MAX_PKG_Diagnostic_LEN))
                    .put("sha256", hash(trees[i]))
                    .put("xml_bytes", trees[i].length)
                    .put("taken_ms", times[i] - started)
                    .put("cache_cleared", clearFlags[i]);
                arr.put(item);
            } catch (Throwable ignored) {
            }
        }
        return arr;
    }

    static void enforceStdoutBudget(JSONObject report) {
        try {
            if (report.toString().length() <= MAX_STDOUT_CHARS) return;
            JSONObject diff = report.optJSONObject("tree_diff");
            if (diff != null) {
                diff.remove("diffs");
                diff.put("truncated", true);
                diff.put("truncation_reason", "stdout_budget_trimmed");
            }
            if (report.toString().length() <= MAX_STDOUT_CHARS) return;
            report.remove("tree_diff");
            if (report.toString().length() <= MAX_STDOUT_CHARS) return;
            report.remove("snapshot_diagnostics");
            if (report.toString().length() <= MAX_STDOUT_CHARS) return;
            // Expected collector errors remain verbatim. Unbounded exception
            // messages must not destroy the entire failure JSON at the reader cap.
            report.put("error", capStr(report.optString("error"), 160));
            report.put("cause", capStr(report.optString("cause"), 160));
            report.put("error_details_truncated", true);
            report.remove("xml_b64");
        } catch (Throwable ignored) {
        }
    }

    // Admission estimate, never a hard bound on an in-flight Binder/dump call.
    static boolean admitExtraSample(long elapsedMs, long budgetMs, long maxSampleMs) {
        return elapsedMs + 300 + 200 + Math.max(1200, maxSampleMs) + 600 + 200 <= budgetMs;
    }

    public static void main(String[] args) {
        int rc=1;Object wrapper=null;Class<?> wc=null;boolean connected=false;File xml=null;
        long started=SystemClock.elapsedRealtime();JSONObject report=new JSONObject();
        byte[][] trees=new byte[2][];int[] windows=new int[2];String[] packages=new String[2];
        long[] times=new long[2];boolean[] clearFlags=new boolean[2];int capturedCount=0;
        int totalTaken=0; boolean sliding=false, exportFailure=false; long budgetMs=0;
        JSONArray allDiagnostics=new JSONArray(), allReadyPolls=new JSONArray();
        try {
            if(args.length<1 || args.length>3 || !args[0].matches("/sdcard/Download/ai-sandbox-direct-[0-9a-f]{32}\\.xml"))
                throw new IllegalArgumentException("invalid_owned_path");
            for(int a=1;a<args.length;a++) {
                if(args[a].equals("--failure-snapshots") && !exportFailure) exportFailure=true;
                else if(args[a].matches("--sliding-ms=[0-9]{4}") && !sliding) {
                    budgetMs=Long.parseLong(args[a].substring(13));
                    if(budgetMs<1000 || budgetMs>6000) throw new IllegalArgumentException("invalid_sliding_budget");
                    sliding=true;
                } else throw new IllegalArgumentException("invalid_collector_option");
            }
            xml=new File(args[0]);report.put("version",1).put("nonce",xml.getName().substring(18,50));
            if(sliding) report.put("sampling_protocol","bounded_sliding_pair_v1").put("collector_budget_ms",budgetMs);
            wc=Class.forName("com.android.uiautomator.core.UiAutomationShellWrapper");wrapper=wc.getConstructor().newInstance();
            wc.getMethod("connect").invoke(wrapper);connected=true;wc.getMethod("setCompressedLayoutHierarchy",boolean.class).invoke(wrapper,false);
            UiAutomation ui=(UiAutomation)wc.getMethod("getUiAutomation").invoke(wrapper);
            try{ui.waitForIdle(100,400);report.put("idle_result","short_idle_reached");}
            catch(TimeoutException e){report.put("idle_result","short_idle_timeout");}
            Class<?> dm=Class.forName("android.hardware.display.DisplayManagerGlobal");Object manager=dm.getMethod("getInstance").invoke(null);
            Display display=(Display)dm.getMethod("getRealDisplay",int.class).invoke(manager,0);Point size=new Point();display.getSize(size);
            Class<?> dumper=Class.forName("com.android.uiautomator.core.AccessibilityNodeInfoDumper");
            int limit=sliding?4:2; long maxSampleMs=0;
            String lastMismatch="unstable_tree"; boolean matched=false;
            for(int k=0;k<limit;k++) {
                long elapsed=SystemClock.elapsedRealtime()-started;
                if(sliding && k>=2 && !admitExtraSample(elapsed,budgetMs,maxSampleMs)) break;
                if(sliding && elapsed>=budgetMs-800) throw new IllegalStateException("collector_sampling_budget_exhausted");
                if(k>0) Thread.sleep(300);
                int i=Math.min(k,1);
                if(k>=2) {
                    trees[0]=trees[1];windows[0]=windows[1];packages[0]=packages[1];times[0]=times[1];clearFlags[0]=clearFlags[1];
                    trees[1]=null;capturedCount=1;
                }
                times[i]=SystemClock.elapsedRealtime();
                long sampleStarted=times[i];
                AccessibilityNodeInfo root=null;
                long rootBudget=(sliding && k>=2)?200:1500;
                long readyDeadline=SystemClock.elapsedRealtime()+rootBudget;
                if(sliding) readyDeadline=Math.min(readyDeadline,started+budgetMs-800);
                int polls=0;
                do {
                    if(sliding && SystemClock.elapsedRealtime()>=readyDeadline) break;
                    boolean cleared=ui.clearCache();root=ui.getRootInActiveWindow();polls++;
                    if(root!=null){clearFlags[i]=cleared;break;}
                    if(SystemClock.elapsedRealtime()>=readyDeadline)break;
                    Thread.sleep(100);
                }while(polls<16);
                if(root==null)throw new IllegalStateException("null_root_after_bounded_readiness");
                try {
                    windows[i]=root.getWindowId();packages[i]=String.valueOf(root.getPackageName());
                    // Never let a failed second file write reuse the first dump's bytes.
                    if(xml.exists() && !xml.delete()) throw new IllegalStateException("owned_xml_reset_failed");
                    if(sliding && SystemClock.elapsedRealtime()-started>=budgetMs-800)
                        throw new IllegalStateException("collector_sampling_budget_exhausted");
                    dumper.getMethod("dumpWindowToFile",AccessibilityNodeInfo.class,File.class,int.class,int.class,int.class)
                        .invoke(null,root,xml,display.getRotation(),size.x,size.y);
                    if(!xml.isFile() || xml.length()==0 || xml.length()>2097152)throw new IllegalStateException("invalid_xml_size");
                    trees[i]=Files.readAllBytes(xml.toPath());capturedCount=i+1;totalTaken++;
                    allReadyPolls.put(polls);
                    JSONObject diag=buildSnapshotDiagnostics(capturedCount,trees,windows,packages,times,clearFlags,started).getJSONObject(i);
                    diag.put("index",totalTaken);allDiagnostics.put(diag);
                } finally{root.recycle();}
                maxSampleMs=Math.max(maxSampleMs,SystemClock.elapsedRealtime()-sampleStarted);
                if(sliding && SystemClock.elapsedRealtime()-started>=budgetMs-600)
                    throw new IllegalStateException("collector_sampling_budget_exhausted");
                if(k==0)continue;
                lastMismatch=(windows[0]<0 || windows[0]!=windows[1] || !packages[0].equals(packages[1]) || packages[0].equals("null"))
                    ?"window_changed":(!clearFlags[0] || !clearFlags[1])?"fresh_cache_reads_not_confirmed":"unstable_tree";
                matched=windows[0]>=0 && windows[0]==windows[1] && !packages[0].equals("null") && packages[0].equals(packages[1])
                    && clearFlags[0] && clearFlags[1] && times[1]-times[0]>=300 && Arrays.equals(trees[0],trees[1]);
                if(matched)break;
            }
            if(!matched)throw new IllegalStateException(lastMismatch);
            JSONArray selectedClears=new JSONArray().put(clearFlags[0]).put(clearFlags[1]);
            JSONArray selectedPolls=new JSONArray().put(allReadyPolls.getInt(totalTaken-2)).put(allReadyPolls.getInt(totalTaken-1));
            report.put("ok",true).put("stable",true).put("snapshot_count",2).put("snapshot_gap_ms",times[1]-times[0])
                .put("cache_clear",selectedClears).put("root_ready_polls",selectedPolls).put("window_id",windows[0]).put("package",packages[0])
                .put("sha256_first",hash(trees[0])).put("sha256_second",hash(trees[1]))
                .put("xml_b64",Base64.encodeToString(trees[1],Base64.NO_WRAP));rc=0;
        }catch(Throwable e){
            try{
                report.put("ok",false).put("error",String.valueOf(e)).put("cause",String.valueOf(e.getCause()));
            }catch(Exception ignored){}
            try{
                JSONArray snapshotDiagnostics=buildSnapshotDiagnostics(capturedCount,trees,windows,packages,times,clearFlags,started);
                if(snapshotDiagnostics.length()>0)report.put("snapshot_diagnostics",snapshotDiagnostics);
            }catch(Throwable ignored){}
            try{
                if(capturedCount==2 && trees[0]!=null && trees[1]!=null){
                    String diffMapJson=UiTreeDiff.summarizeJson(trees[0],trees[1],windows[0],windows[1],packages[0],packages[1]);
                    if(diffMapJson!=null && !diffMapJson.isEmpty()){
                        report.put("tree_diff",new JSONObject(diffMapJson));
                    }
                }
            }catch(Throwable ignored){}
            // Only explicitly requested terminal launch reads export both in-memory trees.
            // The sidecar is separate from the bounded 3800-character failure stdout.
            if (exportFailure && capturedCount==2 && xml!=null &&
                    (String.valueOf(e).contains("unstable_tree") || String.valueOf(e).contains("window_changed"))) {
                File sidecar = new File(xml.getPath()+".failure.bin");
                try {
                    byte[] payload = failureSnapshotBytes(report.getString("nonce"), trees, windows,
                            packages, times, clearFlags, started);
                    if (!sidecar.createNewFile()) throw new IllegalStateException("failure_sidecar_exists");
                    Files.write(sidecar.toPath(), payload);
                    report.put("failure_evidence", new JSONObject().put("status", "available")
                            .put("bytes", payload.length).put("sha256", hash(payload)));
                } catch (Throwable exportError) {
                    // Refusal remains the primary failure even if evidence cannot be saved.
                    try { report.put("failure_evidence", new JSONObject().put("status", "unavailable")); }
                    catch (Throwable ignored) {}
                }
            }
        }finally{
            if(sliding)try {
                report.put("total_snapshots_taken",totalTaken).put("all_snapshot_diagnostics",allDiagnostics)
                    .put("all_root_ready_polls",allReadyPolls);
                if(capturedCount==2) report.put(report.optBoolean("ok",false)?"winning_pair_indices":"failure_pair_indices",
                    new JSONArray().put(totalTaken-1).put(totalTaken));
            }catch(Exception ignored){}
            if(connected)try{wc.getMethod("disconnect").invoke(wrapper);}catch(Exception e){rc=1;}
            if(xml!=null && xml.exists() && !xml.delete())rc=1;
            try{
                report.put("elapsed_ms",SystemClock.elapsedRealtime()-started);
                if(!report.optBoolean("ok",false))enforceStdoutBudget(report);
                System.out.println(report.toString());
            }catch(Exception e){rc=1;}
        }
        System.exit(rc);
    }
}
// END OF FILE: candidate/StableUiRead.java
