// FILE: candidate/UiTreeDiff.java
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Pure-JDK, zero-dependency bounded UI hierarchy XML diff helper for failure diagnostics.
 *
 * Security & resource bounds:
 * - Zero external entity resolution, zero DTD/DOCTYPE network or file access (explicitly rejected).
 * - Operates strictly on in-memory byte[] snapshots already read during the current attempt.
 * - Never emits raw text or content-desc values (reports only length + 8-hex-char SHA-256 prefix).
 * - Hard caps on input byte size, character scan budget, node count, attribute length, and diff items.
 * - Produces a compact, valid JSON string guaranteed to fit inside StableUiRead's failure stdout bound.
 */
public final class UiTreeDiff {
    public static final int MAX_INPUT_BYTES = 2097152; // 2 MiB
    public static final int MAX_SCAN_CHARS = 524288;   // 512 KiB char scan cap
    public static final int MAX_NODES = 600;
    public static final int MAX_DEPTH = 64;
    public static final int MAX_DIFF_ITEMS = 8;
    public static final int MAX_ATTR_VALUE_SCAN = 2048;
    public static final int MAX_ID_OUTPUT_LEN = 64;
    public static final int MAX_CLASS_OUTPUT_LEN = 40;
    public static final int MAX_PATH_OUTPUT_LEN = 64;
    public static final int MAX_JSON_CHARS = 2400;

    private static final String[] TRACKED_DIRECT_ATTRS = new String[] {
        "package", "class", "resource-id", "bounds",
        "enabled", "clickable", "focused", "selected",
        "checkable", "checked", "scrollable", "long-clickable", "password"
    };

    private static final String[] SENSITIVE_TEXT_ATTRS = new String[] {
        "text", "content-desc"
    };

    private UiTreeDiff() {}

    static final class NodeRecord {
        final String path;
        final String pkg;
        final String cls;
        final String resourceId;
        final Map<String, String> trackedAttrs;
        int childCount;

        NodeRecord(String path, Map<String, String> rawAttrs) {
            this.path = cap(path, MAX_PATH_OUTPUT_LEN);
            this.pkg = cap(nonNull(rawAttrs.get("package")), MAX_ID_OUTPUT_LEN);
            this.cls = shortClassName(nonNull(rawAttrs.get("class")));
            this.resourceId = cap(nonNull(rawAttrs.get("resource-id")), MAX_ID_OUTPUT_LEN);
            this.trackedAttrs = new LinkedHashMap<String, String>();
            for (int i = 0; i < TRACKED_DIRECT_ATTRS.length; i++) {
                String k = TRACKED_DIRECT_ATTRS[i];
                String v = rawAttrs.get(k);
                // Compare the entire scanned value; output labels have separate caps.
                this.trackedAttrs.put(k, nonNull(v));
            }
            for (int i = 0; i < SENSITIVE_TEXT_ATTRS.length; i++) {
                String k = SENSITIVE_TEXT_ATTRS[i];
                String v = rawAttrs.get(k);
                this.trackedAttrs.put(k, summarizeSensitiveAttr(v));
            }
            this.childCount = 0;
        }
    }

    static final class ParsedTree {
        boolean parseOk = false;
        boolean truncated = false;
        String truncationReason = null;
        String parseError = null;
        String hierarchyRotation = "";
        final Map<String, NodeRecord> nodesByPath = new LinkedHashMap<String, NodeRecord>();
    }

    static String nonNull(String s) {
        return s == null ? "" : s;
    }

    static String cap(String s, int maxLen) {
        if (s == null) return "";
        return s.length() <= maxLen ? s : s.substring(0, maxLen) + "...";
    }

    static String shortClassName(String cls) {
        if (cls == null || cls.isEmpty()) return "";
        int dot = cls.lastIndexOf('.');
        String base = (dot >= 0 && dot + 1 < cls.length()) ? cls.substring(dot + 1) : cls;
        return cap(base, MAX_CLASS_OUTPUT_LEN);
    }

    static String shortHash8(String value) {
        try {
            byte[] d = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(8);
            for (int i = 0; i < 4; i++) {
                sb.append(String.format("%02x", d[i] & 255));
            }
            return sb.toString();
        } catch (Exception e) {
            return "00000000";
        }
    }

    /**
     * Never outputs raw text or content-desc. Emits len=0 or len=<N>#<8hex>.
     */
    static String summarizeSensitiveAttr(String rawValue) {
        if (rawValue == null || rawValue.isEmpty()) {
            return "len=0";
        }
        return "len=" + rawValue.length() + "#" + shortHash8(rawValue);
    }

    /**
     * Bounded, zero-DTD/zero-entity lexical scanner for uiautomator hierarchy XML.
     */
    static ParsedTree parseHierarchy(byte[] xmlBytes) {
        ParsedTree out = new ParsedTree();
        if (xmlBytes == null || xmlBytes.length == 0) {
            out.parseError = "empty_xml_bytes";
            return out;
        }
        if (xmlBytes.length > MAX_INPUT_BYTES) {
            out.truncated = true;
            out.truncationReason = "input_bytes_exceeded";
            out.parseError = "input_bytes_exceeded";
            return out;
        }

        String xml = new String(xmlBytes, StandardCharsets.UTF_8);
        int len = xml.length();
        int scanLimit = len;
        if (len > MAX_SCAN_CHARS) {
            scanLimit = MAX_SCAN_CHARS;
            xml = xml.substring(0, scanLimit);
            out.truncated = true;
            out.truncationReason = "char_budget_exceeded";
        }

        boolean seenHierarchyOpen = false;
        boolean seenHierarchyClose = false;
        List<String> pathStack = new ArrayList<String>();
        List<Integer> childCounters = new ArrayList<Integer>();
        List<NodeRecord> recordStack = new ArrayList<NodeRecord>();

        int pos = 0;
        while (pos < scanLimit) {
            int lt = xml.indexOf('<', pos);
            if (lt < 0 || lt >= scanLimit) {
                if (!xml.substring(pos, scanLimit).trim().isEmpty()) {
                    out.parseError = "unexpected_xml_text";
                    return out;
                }
                break;
            }

            // Hierarchy dumps contain no character data between tags.
            {
                for (int k = pos; k < lt; k++) {
                    char ch = xml.charAt(k);
                    if (ch != '\uFEFF' && !Character.isWhitespace(ch)) {
                        out.parseError = "unexpected_xml_text";
                        return out;
                    }
                }
            }

            if (lt + 1 >= scanLimit) {
                out.parseError = "truncated_tag";
                return out;
            }

            char next = xml.charAt(lt + 1);
            if (seenHierarchyClose && next != '!' && next != '?') {
                out.parseError = "content_after_hierarchy";
                return out;
            }
            if (next == '?') {
                int closePi = xml.indexOf("?>", lt + 2);
                if (closePi < 0 || closePi >= scanLimit) {
                    out.parseError = "unclosed_xml_declaration";
                    return out;
                }
                pos = closePi + 2;
                continue;
            }

            if (next == '!') {
                if (xml.startsWith("!--", lt + 1)) {
                    int closeComment = xml.indexOf("-->", lt + 4);
                    if (closeComment < 0 || closeComment >= scanLimit) {
                        out.parseError = "unclosed_comment";
                        return out;
                    }
                    pos = closeComment + 3;
                    continue;
                }
                // Explicitly reject DOCTYPE / ENTITY / CDATA or any other declaration
                out.parseError = "doctype_or_entity_forbidden";
                return out;
            }

            if (next == '/') {
                int gt = xml.indexOf('>', lt + 2);
                if (gt < 0 || gt >= scanLimit) {
                    out.parseError = "unclosed_end_tag";
                    return out;
                }
                String closeName = xml.substring(lt + 2, gt).trim();
                if ("node".equals(closeName)) {
                    if (pathStack.isEmpty()) {
                        out.parseError = "unbalanced_node_end_tag";
                        return out;
                    }
                    pathStack.remove(pathStack.size() - 1);
                    childCounters.remove(childCounters.size() - 1);
                    recordStack.remove(recordStack.size() - 1);
                } else if ("hierarchy".equals(closeName)) {
                    if (!seenHierarchyOpen || !pathStack.isEmpty()) {
                        out.parseError = "unbalanced_hierarchy_end_tag";
                        return out;
                    }
                    seenHierarchyClose = true;
                    pos = gt + 1;
                    continue;
                } else {
                    out.parseError = "unexpected_end_tag:" + cap(closeName, 24);
                    return out;
                }
                pos = gt + 1;
                continue;
            }

            // Parse start or self-closing tag
            int nameStart = lt + 1;
            int cursor = nameStart;
            while (cursor < scanLimit) {
                char c = xml.charAt(cursor);
                if (Character.isWhitespace(c) || c == '/' || c == '>') break;
                cursor++;
            }
            String tagName = xml.substring(nameStart, cursor);
            if (!"hierarchy".equals(tagName) && !"node".equals(tagName)) {
                out.parseError = "unexpected_tag:" + cap(tagName, 24);
                return out;
            }

            Map<String, String> attrs = new LinkedHashMap<String, String>();
            boolean selfClosing = false;
            boolean tagClosed = false;

            while (cursor < scanLimit) {
                while (cursor < scanLimit && Character.isWhitespace(xml.charAt(cursor))) cursor++;
                if (cursor >= scanLimit) break;
                char c = xml.charAt(cursor);
                if (c == '>') {
                    cursor++;
                    tagClosed = true;
                    break;
                }
                if (c == '/') {
                    if (cursor + 1 < scanLimit && xml.charAt(cursor + 1) == '>') {
                        selfClosing = true;
                        tagClosed = true;
                        cursor += 2;
                        break;
                    } else {
                        out.parseError = "malformed_self_close";
                        return out;
                    }
                }

                int attrStart = cursor;
                while (cursor < scanLimit) {
                    char ac = xml.charAt(cursor);
                    if (ac == '=' || Character.isWhitespace(ac) || ac == '>' || ac == '/') break;
                    cursor++;
                }
                String attrName = xml.substring(attrStart, cursor);
                if (attrName.isEmpty() || attrName.length() > 64 || attrs.size() >= 64) {
                    out.parseError = "invalid_attribute_name_or_count";
                    return out;
                }
                while (cursor < scanLimit && Character.isWhitespace(xml.charAt(cursor))) cursor++;
                if (cursor >= scanLimit || xml.charAt(cursor) != '=') {
                    out.parseError = "missing_attr_equals";
                    return out;
                }
                cursor++; // skip '='
                while (cursor < scanLimit && Character.isWhitespace(xml.charAt(cursor))) cursor++;
                if (cursor >= scanLimit) {
                    out.parseError = "missing_attr_quote";
                    return out;
                }
                char quote = xml.charAt(cursor);
                if (quote != '"' && quote != '\'') {
                    out.parseError = "invalid_attr_quote";
                    return out;
                }
                cursor++;
                int valStart = cursor;
                int valEnd = xml.indexOf(quote, valStart);
                if (valEnd < 0 || valEnd >= scanLimit) {
                    out.parseError = "unclosed_attr_value";
                    return out;
                }
                String rawVal = xml.substring(valStart, valEnd);
                if (rawVal.indexOf('<') >= 0 || attrs.containsKey(attrName)) {
                    out.parseError = "invalid_or_duplicate_attribute";
                    return out;
                }
                // Validate entities before truncation; an entity beyond the stored
                // prefix must not bypass the no-custom-entity rule.
                String decodedVal = decodeXmlEntities(rawVal);
                if (decodedVal == null) {
                    out.parseError = "disallowed_xml_entity";
                    return out;
                }
                if (rawVal.length() > MAX_ATTR_VALUE_SCAN) {
                    decodedVal = decodedVal.substring(0, Math.min(decodedVal.length(), MAX_ATTR_VALUE_SCAN));
                    if (!out.truncated) {
                        out.truncated = true;
                        out.truncationReason = "attr_value_truncated";
                    }
                }
                attrs.put(attrName, decodedVal);
                cursor = valEnd + 1;
                if (cursor < scanLimit && !Character.isWhitespace(xml.charAt(cursor))
                        && xml.charAt(cursor) != '/' && xml.charAt(cursor) != '>') {
                    out.parseError = "missing_attribute_separator";
                    return out;
                }
            }

            if (!tagClosed) {
                out.parseError = "unclosed_start_tag";
                return out;
            }

            if ("hierarchy".equals(tagName)) {
                if (seenHierarchyOpen) {
                    out.parseError = "multiple_hierarchy_roots";
                    return out;
                }
                seenHierarchyOpen = true;
                out.hierarchyRotation = cap(nonNull(attrs.get("rotation")), 16);
                if (selfClosing) {
                    seenHierarchyClose = true;
                    pos = cursor;
                    continue;
                }
            } else {
                if (!seenHierarchyOpen) {
                    out.parseError = "node_before_hierarchy";
                    return out;
                }
                if (pathStack.size() >= MAX_DEPTH) {
                    out.truncated = true;
                    out.truncationReason = "depth_limit_exceeded";
                    out.parseOk = true;
                    return out;
                }
                if (out.nodesByPath.size() >= MAX_NODES) {
                    out.truncated = true;
                    out.truncationReason = "node_limit_exceeded";
                    out.parseOk = true;
                    return out;
                }

                String currentPath;
                if (pathStack.isEmpty()) {
                    int rootIdx = childCounters.isEmpty() ? 0 : childCounters.get(0);
                    if (childCounters.isEmpty()) {
                        childCounters.add(1);
                    } else {
                        childCounters.set(0, rootIdx + 1);
                    }
                    currentPath = rootIdx == 0 ? "R" : ("R_" + rootIdx);
                } else {
                    int parentLevel = childCounters.size() - 1;
                    int childIdx = childCounters.get(parentLevel);
                    childCounters.set(parentLevel, childIdx + 1);
                    NodeRecord parentRec = recordStack.get(recordStack.size() - 1);
                    parentRec.childCount++;
                    currentPath = pathStack.get(pathStack.size() - 1) + "." + childIdx;
                }

                NodeRecord rec = new NodeRecord(currentPath, attrs);
                out.nodesByPath.put(currentPath, rec);

                if (!selfClosing) {
                    pathStack.add(currentPath);
                    childCounters.add(0);
                    recordStack.add(rec);
                }
            }
            pos = cursor;
        }

        if (out.truncated && "char_budget_exceeded".equals(out.truncationReason)) {
            out.parseOk = seenHierarchyOpen && !out.nodesByPath.isEmpty();
            if (!out.parseOk && out.parseError == null) {
                out.parseError = "truncated_before_valid_nodes";
            }
            return out;
        }

        if (!seenHierarchyOpen || !seenHierarchyClose || !pathStack.isEmpty()) {
            out.parseError = "incomplete_hierarchy_tree";
            return out;
        }

        out.parseOk = true;
        return out;
    }

    /**
     * Decodes only the 5 standard XML predefined entities and numeric character references.
     * Rejects any custom/external general entity (&foo;) by returning null.
     */
    static String decodeXmlEntities(String s) {
        if (s == null || s.indexOf('&') < 0) return s;
        StringBuilder sb = new StringBuilder(s.length());
        int len = s.length();
        int i = 0;
        while (i < len) {
            char c = s.charAt(i);
            if (c != '&') {
                sb.append(c);
                i++;
                continue;
            }
            int semi = s.indexOf(';', i + 1);
            if (semi < 0 || semi - i > 12) {
                return null;
            }
            String ent = s.substring(i + 1, semi);
            if ("lt".equals(ent)) sb.append('<');
            else if ("gt".equals(ent)) sb.append('>');
            else if ("amp".equals(ent)) sb.append('&');
            else if ("quot".equals(ent)) sb.append('"');
            else if ("apos".equals(ent)) sb.append('\'');
            else if (ent.startsWith("#")) {
                try {
                    int codePoint = (ent.startsWith("#x") || ent.startsWith("#X"))
                            ? Integer.parseInt(ent.substring(2), 16)
                            : Integer.parseInt(ent.substring(1), 10);
                    if (!(codePoint == 9 || codePoint == 10 || codePoint == 13
                            || (codePoint >= 0x20 && codePoint <= 0xD7FF)
                            || (codePoint >= 0xE000 && codePoint <= 0xFFFD)
                            || (codePoint >= 0x10000 && codePoint <= 0x10FFFF))) return null;
                    sb.appendCodePoint(codePoint);
                } catch (Exception e) {
                    return null;
                }
            } else {
                // Custom named entity -> reject
                return null;
            }
            i = semi + 1;
        }
        return sb.toString();
    }

    /**
     * Computes a bounded JSON string summarizing structural and attribute differences
     * between two in-memory XML byte snapshots.
     */
    public static String summarizeJson(
            byte[] firstXml,
            byte[] secondXml,
            int winFirst,
            int winSecond,
            String pkgFirst,
            String pkgSecond) {
        boolean rawEqual = (firstXml != null && secondXml != null && Arrays.equals(firstXml, secondXml));
        ParsedTree t1 = parseHierarchy(firstXml);
        ParsedTree t2 = parseHierarchy(secondXml);

        String p1 = nonNull(pkgFirst);
        String p2 = nonNull(pkgSecond);
        boolean windowIdDiff = (winFirst < 0 || winSecond < 0 || winFirst != winSecond);
        boolean packageDiff = (!p1.equals(p2) || "null".equals(p1) || "null".equals(p2));

        if (!t1.parseOk || !t2.parseOk) {
            StringBuilder b = new StringBuilder(256);
            b.append('{');
            appendKv(b, "schema", "ui_tree_diff_v2", true);
            appendKv(b, "category", "parse_error", true);
            appendBool(b, "raw_bytes_equal", rawEqual, true);
            appendBool(b, "parse_ok", false, true);
            appendBool(b, "truncated", t1.truncated || t2.truncated, true);
            appendKv(b, "parse_error_first", nonNull(t1.parseError), true);
            appendKv(b, "parse_error_second", nonNull(t2.parseError), false);
            b.append('}');
            return b.toString();
        }

        boolean sameStructure = (t1.nodesByPath.size() == t2.nodesByPath.size());
        int addedCount = 0;
        int removedCount = 0;
        int modifiedNodeCount = 0;
        int childCountChangedNodes = 0;
        Map<String, Integer> attrHistogram = new LinkedHashMap<String, Integer>();
        List<String> diffEntriesJson = new ArrayList<String>();
        int totalDiffNodes = 0;

        // 1. Check nodes in first vs second
        for (Map.Entry<String, NodeRecord> e : t1.nodesByPath.entrySet()) {
            String path = e.getKey();
            NodeRecord n1 = e.getValue();
            NodeRecord n2 = t2.nodesByPath.get(path);
            if (n2 == null) {
                sameStructure = false;
                removedCount++;
                totalDiffNodes++;
                if (diffEntriesJson.size() < MAX_DIFF_ITEMS) {
                    diffEntriesJson.add(buildDiffItemJson("removed_node", n1, null, null));
                }
                continue;
            }
            if (n1.childCount != n2.childCount) {
                sameStructure = false;
                childCountChangedNodes++;
            }
            List<String> changedAttrs = new ArrayList<String>();
            for (Map.Entry<String, String> ae : n1.trackedAttrs.entrySet()) {
                String k = ae.getKey();
                String v1 = nonNull(ae.getValue());
                String v2 = nonNull(n2.trackedAttrs.get(k));
                if (!v1.equals(v2)) {
                    increment(attrHistogram, k);
                    changedAttrs.add(formatAttrDelta(k, v1, v2));
                }
            }
            if (n1.childCount != n2.childCount) {
                increment(attrHistogram, "child_count");
                changedAttrs.add("children:" + n1.childCount + "->" + n2.childCount);
            }
            if (!changedAttrs.isEmpty()) {
                modifiedNodeCount++;
                totalDiffNodes++;
                if (diffEntriesJson.size() < MAX_DIFF_ITEMS) {
                    String kind = (n1.childCount != n2.childCount) ? "node_and_children_changed" : "attr_changed";
                    diffEntriesJson.add(buildDiffItemJson(kind, n1, n2, changedAttrs));
                }
            }
        }

        // 2. Check nodes added in second
        for (Map.Entry<String, NodeRecord> e : t2.nodesByPath.entrySet()) {
            String path = e.getKey();
            if (!t1.nodesByPath.containsKey(path)) {
                sameStructure = false;
                addedCount++;
                totalDiffNodes++;
                if (diffEntriesJson.size() < MAX_DIFF_ITEMS) {
                    diffEntriesJson.add(buildDiffItemJson("added_node", null, e.getValue(), null));
                }
            }
        }

        String category;
        if (packageDiff) {
            category = "package_changed";
        } else if (windowIdDiff) {
            category = "window_id_changed";
        } else if (rawEqual) {
            category = "identical";
        } else if (t1.truncated || t2.truncated) {
            category = "incomplete_tree";
        } else if (!sameStructure || addedCount > 0 || removedCount > 0 || childCountChangedNodes > 0) {
            category = "structure_changed";
        } else if (modifiedNodeCount > 0) {
            category = "attributes_only";
        } else {
            category = "raw_bytes_only";
        }

        int omittedDiffs = Math.max(0, totalDiffNodes - diffEntriesJson.size());
        boolean truncated = t1.truncated || t2.truncated || (omittedDiffs > 0);
        String truncReason = t1.truncationReason != null ? t1.truncationReason
                : (t2.truncationReason != null ? t2.truncationReason
                : (omittedDiffs > 0 ? "diff_item_limit_exceeded" : ""));

        String fullJson = buildSummaryObjectJson(
                category, rawEqual, truncated, truncReason,
                t1.nodesByPath.size(), t2.nodesByPath.size(),
                sameStructure, addedCount, removedCount, modifiedNodeCount,
                totalDiffNodes, omittedDiffs, attrHistogram, diffEntriesJson);

        if (fullJson.length() <= MAX_JSON_CHARS) {
            return fullJson;
        }

        // Fallback if diff list exceeds MAX_JSON_CHARS: trim diff list to 3 items, then 0 items
        List<String> shorter = diffEntriesJson.size() > 3 ? diffEntriesJson.subList(0, 3) : new ArrayList<String>();
        int newOmitted = Math.max(0, totalDiffNodes - shorter.size());
        String trimmedJson = buildSummaryObjectJson(
                category, rawEqual, true, "json_size_cap_exceeded",
                t1.nodesByPath.size(), t2.nodesByPath.size(),
                sameStructure, addedCount, removedCount, modifiedNodeCount,
                totalDiffNodes, newOmitted, attrHistogram, shorter);
        if (trimmedJson.length() <= MAX_JSON_CHARS) {
            return trimmedJson;
        }
        return buildSummaryObjectJson(
                category, rawEqual, true, "json_size_cap_exceeded",
                t1.nodesByPath.size(), t2.nodesByPath.size(),
                sameStructure, addedCount, removedCount, modifiedNodeCount,
                totalDiffNodes, totalDiffNodes, attrHistogram, new ArrayList<String>());
    }

    private static String buildSummaryObjectJson(
            String category,
            boolean rawEqual,
            boolean truncated,
            String truncReason,
            int count1,
            int count2,
            boolean sameStructure,
            int addedCount,
            int removedCount,
            int modifiedCount,
            int totalDiffNodes,
            int omittedDiffs,
            Map<String, Integer> attrHistogram,
            List<String> diffItemsJson) {
        StringBuilder b = new StringBuilder(512);
        b.append('{');
        appendKv(b, "schema", "ui_tree_diff_v2", true);
        appendKv(b, "category", category, true);
        appendBool(b, "raw_bytes_equal", rawEqual, true);
        appendBool(b, "parse_ok", true, true);
        appendBool(b, "truncated", truncated, true);
        if (truncated && truncReason != null && !truncReason.isEmpty()) {
            appendKv(b, "truncation_reason", truncReason, true);
        }
        appendInt(b, "node_count_first", count1, true);
        appendInt(b, "node_count_second", count2, true);
        appendBool(b, "same_structure", sameStructure, true);
        appendInt(b, "added_nodes", addedCount, true);
        appendInt(b, "removed_nodes", removedCount, true);
        appendInt(b, "modified_nodes", modifiedCount, true);
        appendInt(b, "total_diff_nodes", totalDiffNodes, true);
        appendInt(b, "omitted_diff_count", omittedDiffs, true);

        b.append("\"changed_attribute_counts\":{");
        boolean firstHist = true;
        for (Map.Entry<String, Integer> he : attrHistogram.entrySet()) {
            if (!firstHist) b.append(',');
            firstHist = false;
            b.append('"').append(escapeJson(he.getKey())).append("\":").append(he.getValue());
        }
        b.append("},");

        b.append("\"diffs\":[");
        for (int i = 0; i < diffItemsJson.size(); i++) {
            if (i > 0) b.append(',');
            b.append(diffItemsJson.get(i));
        }
        b.append("]}");
        return b.toString();
    }

    private static String buildDiffItemJson(
            String kind,
            NodeRecord n1,
            NodeRecord n2,
            List<String> changedAttrs) {
        NodeRecord ref = (n2 != null) ? n2 : n1;
        StringBuilder b = new StringBuilder(128);
        b.append('{');
        appendKv(b, "path", ref.path, true);
        appendKv(b, "kind", kind, true);
        appendKv(b, "class", ref.cls, true);
        appendKv(b, "resource_id", ref.resourceId, changedAttrs != null && !changedAttrs.isEmpty());
        if (changedAttrs != null && !changedAttrs.isEmpty()) {
            b.append("\"attrs\":[");
            int limit = Math.min(changedAttrs.size(), 6);
            for (int i = 0; i < limit; i++) {
                if (i > 0) b.append(',');
                b.append('"').append(escapeJson(cap(changedAttrs.get(i), 48))).append('"');
            }
            b.append(']');
        }
        b.append('}');
        return b.toString();
    }

    private static String formatAttrDelta(String attrName, String v1, String v2) {
        if ("text".equals(attrName) || "content-desc".equals(attrName)) {
            return attrName + ":" + v1 + "->" + v2;
        }
        if ("bounds".equals(attrName) || "focused".equals(attrName) || "enabled".equals(attrName)
                || "selected".equals(attrName) || "checked".equals(attrName) || "clickable".equals(attrName)
                || "scrollable".equals(attrName)) {
            return attrName + ":" + cap(v1, 20) + "->" + cap(v2, 20);
        }
        return attrName + ":changed";
    }

    private static void increment(Map<String, Integer> map, String key) {
        Integer cur = map.get(key);
        map.put(key, cur == null ? 1 : cur + 1);
    }

    private static void appendKv(StringBuilder b, String k, String v, boolean comma) {
        b.append('"').append(escapeJson(k)).append("\":\"").append(escapeJson(v)).append('"');
        if (comma) b.append(',');
    }

    private static void appendInt(StringBuilder b, String k, int v, boolean comma) {
        b.append('"').append(escapeJson(k)).append("\":").append(v);
        if (comma) b.append(',');
    }

    private static void appendBool(StringBuilder b, String k, boolean v, boolean comma) {
        b.append('"').append(escapeJson(k)).append("\":").append(v ? "true" : "false");
        if (comma) b.append(',');
    }

    static String escapeJson(String s) {
        if (s == null) return "";
        StringBuilder sb = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\b': sb.append("\\b"); break;
                case '\f': sb.append("\\f"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        return sb.toString();
    }
}
// END OF FILE: candidate/UiTreeDiff.java
