package net.agentmedia.companion;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Put a question to the conversation that has been talking to you.
 *
 * The one thing this app can send that is not transport. Everything else on
 * the home screen changes what a player is doing; this asks something, and the
 * answer comes back the way every reply already does — spoken, out of this
 * phone, minutes later.
 *
 * Nothing about *which* conversation is decided here, and that is deliberate.
 * A conversation is a tmux pane on the hub and a transcript beside it; a phone
 * has neither, and the speech history it keeps locally stopped being anything
 * in July. So both calls are questions to the far side, which resolves the
 * conversation from the tags every speech row already carries and answers with
 * a sentence fit to show someone.
 *
 * The status call exists so the question is never typed into the void. That is
 * the failure worth designing out: the ask goes nowhere, nothing says so, and
 * the answer that never arrives reads as the feature being broken.
 *
 * {@code android.*}-free, so {@code test/run.sh} covers it against a fake
 * listener rather than a sideload and a squint at the phone screen.
 */
final class AskRequest {

    /** Who would be asked, and whether they are still there. */
    static final class Status {
        /** A conversation is going and would take the question. */
        final boolean live;
        /** What to call it — the tmux window name, usually. */
        final String label;
        /** A sentence saying how it stands, fit to show someone. */
        final String reason;
        /** False when the hub could not be reached at all. */
        final boolean reachable;
        /** The last thing it said, for the dialog's second line. */
        final String last;
        /** What is playing, as a name — what a fresh conversation is about. */
        final String subject;

        Status(boolean live, String label, String reason, boolean reachable,
               String last, String subject) {
            this.live = live;
            this.label = label;
            this.reason = reason;
            this.reachable = reachable;
            this.last = last;
            this.subject = subject;
        }

        /**
         * Whether there is any point opening the box.
         *
         * Nobody listening is no longer a dead end: the far side starts a
         * conversation named for what is playing, and names it so that the
         * next question lands in it. The one thing that is still a dead end is
         * a hub that cannot be reached, because nothing on this phone can
         * answer anything.
         */
        boolean canAsk() {
            return live || reachable;
        }

        /** The dialog's title: who is being asked, or what is being asked about. */
        String title() {
            if (live) return "ask " + (label.isEmpty() ? "the conversation" : label);
            if (!reachable) return reason.isEmpty() ? "agent-media is not answering" : reason;
            return subject.isEmpty() ? "ask about this"
                                     : "ask about " + subject;
        }

        /**
         * The line under the title, when this would start something.
         *
         * The reason belongs on screen and not swallowed: "deploy has closed"
         * explains why the answer will come from somewhere that knows nothing
         * about the last hour of conversation, which is worth knowing before
         * the question is phrased.
         */
        String note() {
            if (live || !reachable) return "";
            return reason.isEmpty() ? "a new conversation"
                                    : reason + " — this starts a new one";
        }
    }

    /** What to show after sending: one line, and whether it went well. */
    static final class Result {
        final boolean ok;
        final String message;

        Result(boolean ok, String message) {
            this.ok = ok;
            this.message = message;
        }
    }

    private AskRequest() {}

    static String body(String question, String channel) {
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        m.put("question", question == null ? "" : question);
        if (channel != null && !channel.isEmpty()) m.put("channel", channel);
        // How the line is tagged where it lands. A submitted line arrives as a
        // user message and is otherwise indistinguishable from David typing it
        // at the keyboard — which invites the session to answer as though he
        // were sitting there, when he is holding a phone and the reply has to
        // be spoken.
        m.put("via", "the phone");
        return Json.write(m);
    }

    static Status parseStatus(int status, String payload) {
        try {
            Map<String, Object> o = Json.parseObject(payload);
            boolean reachable = Json.asBool(o.get("reachable"), true);
            return new Status(Json.asBool(o.get("live"), false) && status == 200,
                              str(o.get("label")), str(o.get("reason")),
                              reachable && status == 200, str(o.get("last")),
                              str(o.get("subject")));
        } catch (RuntimeException e) {
            return new Status(false, "", "agent-media is not answering", false,
                              "", "");
        }
    }

    /**
     * The answer to a sent question, as one line.
     *
     * A refusal comes back 200 with {@code ok:false} — "that conversation has
     * closed" is information, not a transport failure, and the two have to stay
     * tellable apart because only one of them is worth trying again.
     */
    static Result parse(int status, String payload) {
        String line = "";
        boolean ok = false;
        try {
            Map<String, Object> o = Json.parseObject(payload);
            ok = Json.asBool(o.get("ok"), false) || Json.asBool(o.get("asked"), false);
            line = str(o.get("error"));
            if (line.isEmpty()) line = str(o.get("reason"));
            if (ok) {
                // A started conversation reports the window it opened, which
                // is a more useful thing to show than "asked": it is where the
                // answer is being written, and what the next question will
                // find.
                String started = str(o.get("started"));
                String label = started.isEmpty() ? str(o.get("label")) : started;
                line = label.isEmpty() ? "asked" : "asked " + label;
            }
        } catch (RuntimeException e) {
            line = "";
            ok = false;
        }
        if (line.isEmpty()) {
            line = ok ? "asked" : "the ask failed (HTTP " + status + ")";
        }
        return new Result(ok && status == 200, line);
    }

    /** Who would be asked. Never throws. */
    static Status status(Server server, String channel) {
        String c = channel == null || channel.isEmpty() ? "speech" : channel;
        Loopback.Reply r = Loopback.get(server, "/ask?channel=" + c);
        if (!r.reached()) return new Status(false, "", r.failure, false, "", "");
        if (r.refused()) return new Status(false, "", Loopback.REFUSED, false, "", "");
        return parseStatus(r.status, r.body);
    }

    /** Send it. Never throws — the caller has a toast to show. */
    static Result send(Server server, String question, String channel) {
        Loopback.Reply r = Loopback.post(server, "/ask", body(question, channel));
        if (!r.reached()) return new Result(false, r.failure);
        if (r.refused()) return new Result(false, Loopback.REFUSED);
        return parse(r.status, r.body);
    }

    private static String str(Object v) {
        String s = Json.asString(v);
        return s == null ? "" : s;
    }
}
