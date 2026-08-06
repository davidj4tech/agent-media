;;; am-control.el --- The agent-media control contract -*- lexical-binding: t; -*-

;; Author: David
;; Package-Requires: ((emacs "27.1"))

;;; Commentary:

;; The one place that talks to agent-media.  Adapters (empv, listen.el, EMMS)
;; call the `am-control-*' commands below and nothing else; this file is the
;; only one permitted to shell out.  That single rule is what makes an adapter
;; reviewable in isolation and removable without a trace.
;;
;; The surface ORCHESTRATES.  It never owns audio: no mpv is started here, no
;; MPD is spoken to, no persistent state is written.  Everything below the
;; contract line — acquisition on the phone's residential IP, playout, the
;; duck/pause policy, the state store — stays exactly as it was.
;;
;; Dispatch is per-ACTION, not per-host (see docs/control-surface.md §6):
;;
;;   local   toggle/next/prev/seek/hold/release/status — these only touch the
;;           local mpv, and agent-media live-probes the player rather than
;;           trusting its DB, so a local action is picked up by the other host
;;           on its next poll with no synchronisation.
;;   remote  play/queue-add — these need the library, content-type policy and
;;           history, so they are a round trip whichever host you sit on.
;;
;; The same file runs unmodified on red5 and on the phone; only the command
;; prefixes below differ.  Set `am-control-local-actions' to nil to force
;; everything remote, which reproduces a pure red5 surface exactly.

;;; Code:

(require 'json)
(require 'subr-x)
(require 'cl-lib)

(defgroup am-control nil
  "Control surface for the agent-media music channel."
  :group 'multimedia
  :prefix "am-control-")

;; am-control-mpv is loaded on demand (it is a pure optimisation, and the CLI
;; path must work without it), so declare its surface for the byte compiler.
(declare-function am-control-mpv-usable-p "am-control-mpv" ())
(declare-function am-control-mpv-command "am-control-mpv" (&rest args))
(declare-function am-control-mpv-get-properties "am-control-mpv" (props))
(declare-function am-control-mpv-invalidate "am-control-mpv" ())
(defvar am-control-prefer-direct)


;;; Dispatch configuration

(defcustom am-control-local-command '("media")
  "Command prefix for actions that only touch the local player."
  :type '(repeat string))

(defcustom am-control-remote-command '("ssh" "red5" "media")
  "Command prefix for actions that must originate where the state store is.
On red5 itself, set this to `(\"media\")'."
  :type '(repeat string))

(defcustom am-control-local-hold-command '("media-call-guard")
  "Command prefix for the local call-guard hold trigger.
On the phone `media-call-guard' is on PATH; on red5 it lives in the venv
\(`~/projects/agent-media/.venv/bin/media-call-guard')."
  :type '(repeat string))

(defcustom am-control-remote-hold-command
  '("ssh" "red5" "/home/ryer/projects/agent-media/.venv/bin/media-call-guard")
  "Command prefix for the call-guard hold trigger on the remote host."
  :type '(repeat string))

(defcustom am-control-local-actions
  '(toggle next prev seek hold release status)
  "Actions dispatched locally rather than to the remote host.
Set to nil to force every action remote — the escape hatch if the
per-action split ever proves troublesome."
  :type '(repeat symbol))

(defcustom am-control-status-timeout 3
  "Seconds to wait for a `status' read before giving up.
A poller must never wedge the UI; on timeout the last snapshot stands."
  :type 'number)

(defcustom am-control-debug nil
  "When non-nil, log every dispatched command and non-zero exit to *am-control*."
  :type 'boolean)


;;; call-guard's hold flag
;;
;; The flag file is call-guard's *documented* external trigger — "any external
;; trigger can pause+duck playback by touching a flag file" (call_guard.py) —
;; so reading and touching it is using the supported interface, not reaching
;; around one. call-guard keeps ownership of what a hold actually does: it
;; debounces, ducks music, pauses speech, and auto-resumes on release.

(defcustom am-control-hold-flag nil
  "Path to call-guard's external-hold flag file.
nil means resolve it the way call_guard.py does: `MEDIA_CALL_GUARD_HOLD_FLAG'
from the environment, else `$XDG_STATE_HOME/agent-media/call-guard.hold'."
  :type '(choice (const :tag "Resolve like call_guard.py" nil) file))

(defun am-control-hold-flag-path ()
  "Resolved path of call-guard's hold flag."
  (or am-control-hold-flag
      (getenv "MEDIA_CALL_GUARD_HOLD_FLAG")
      (expand-file-name
       "agent-media/call-guard.hold"
       (or (getenv "XDG_STATE_HOME")
           (expand-file-name ".local/state" (or (getenv "HOME") "~"))))))

(defun am-control-hold-flag-present-p ()
  "Non-nil when a hold is currently engaged."
  (file-exists-p (am-control-hold-flag-path)))


;;; Plumbing — the only code here that shells out

(defun am-control--prefix (action kind)
  "Command prefix for ACTION.  KIND is `media' or `hold'."
  (let ((local (memq action am-control-local-actions)))
    (pcase kind
      ('hold (if local am-control-local-hold-command
               am-control-remote-hold-command))
      (_     (if local am-control-local-command
               am-control-remote-command)))))

(defun am-control--log (fmt &rest args)
  (when am-control-debug
    (with-current-buffer (get-buffer-create "*am-control*")
      (goto-char (point-max))
      (insert (apply #'format fmt args) "\n"))))

(defun am-control--run (action argv)
  "Run ARGV asynchronously for ACTION.  Fire-and-forget.

Asynchronous on purpose: a remote action crosses the network, and blocking
Emacs on it would make the surface feel worse than the CLI it replaces.
Adapters should therefore be optimistic — show the intent immediately and
reconcile on the next status poll."
  (am-control--log "run %s: %S" action argv)
  (make-process
   :name (format "am-control-%s" action)
   :command argv
   :noquery t
   :connection-type 'pipe
   :buffer (and am-control-debug (get-buffer-create "*am-control*"))
   :sentinel
   (lambda (proc event)
     (let ((status (process-exit-status proc)))
       (when (and (memq (process-status proc) '(exit signal))
                  (not (zerop status)))
         (message "am-control: %s failed (%s) %s"
                  action status (string-trim (or event "")))))))
  nil)

(defun am-control--run-sync (action argv)
  "Run ARGV for ACTION synchronously.  Return stdout, or nil on failure.
Only `status' uses this — every other action is fire-and-forget."
  (am-control--log "run-sync %s: %S" action argv)
  (with-temp-buffer
    (let ((code (condition-case err
                    (apply #'call-process (car argv) nil t nil (cdr argv))
                  (error (am-control--log "error: %S" err) nil))))
      (when (eq code 0)
        (buffer-string)))))

(defun am-control--media (action &rest args)
  (am-control--run action (append (am-control--prefix action 'media) args)))

(defun am-control--direct (action mpv-args)
  "Try ACTION over mpv's socket directly.  Non-nil if it was handled.

Only for actions the CLI implements as a bare backend call — verified in
cli.py's transport block, which writes no state for toggle/next/prev/seek.
Anything touching volume, the library or history must not come through here.

Also requires ACTION to be dispatched locally: if the user has routed it to
the remote host, honour that rather than quietly short-circuiting it."
  (and (memq action am-control-local-actions)
       (require 'am-control-mpv nil t)
       (am-control-mpv-usable-p)
       (prog1 (apply #'am-control-mpv-command mpv-args)
         (am-control--log "direct %s: %S" action mpv-args))))


;;; The contract — nine actions

;;;###autoload
(defun am-control-play (uri &optional where as)
  "Play URI on the music channel, replacing the queue.
WHERE is passed through opaquely (auto/phone/rooms/local); AS sets the
interruption content type (music/audiobook/podcast/dj-set/ambient)."
  (interactive "sURI: ")
  (apply #'am-control--media 'play "music" "play"
         (append (when where (list "--where" where))
                 (when as (list "--as" as))
                 (list uri))))

;;;###autoload
(defun am-control-queue-add (uri &optional where)
  "Append URI to the music queue without clearing what is playing."
  (interactive "sURI: ")
  (apply #'am-control--media 'queue-add "music" "play" "--add"
         (append (when where (list "--where" where)) (list uri))))

;;;###autoload
(defun am-control-toggle ()
  "Toggle pause/resume on the music channel."
  (interactive)
  (or (am-control--direct 'toggle '("cycle" "pause"))
      (am-control--media 'toggle "music" "toggle")))

;;;###autoload
(defun am-control-next ()
  "Skip to the next track."
  (interactive)
  (or (am-control--direct 'next '("playlist-next" "weak"))
      (am-control--media 'next "music" "next"))
  (when (fboundp 'am-control-mpv-invalidate) (am-control-mpv-invalidate)))

;;;###autoload
(defun am-control-prev (&optional restart)
  "Go to the previous track.  With RESTART, restart the current one first."
  (interactive "P")
  ;; `--restart-first' carries real logic (restart if past the track's start,
  ;; within a grace window), so it always goes to the CLI.
  (if restart
      (am-control--media 'prev "music" "prev" "--restart-first")
    (or (am-control--direct 'prev '("playlist-prev" "weak"))
        (am-control--media 'prev "music" "prev")))
  (when (fboundp 'am-control-mpv-invalidate) (am-control-mpv-invalidate)))

(defun am-control--relative-seconds (spec)
  "Seconds for SPEC if it is a plain signed second count, else nil.
Only this simplest form is fast-pathed; timecodes and absolute jumps keep
the CLI's `_do_timecode_seek' parsing rather than reimplementing it here."
  (when (string-match "\\`\\([-+]\\)\\([0-9]+\\)\\'" (string-trim spec))
    (let ((n (string-to-number (match-string 2 spec))))
      (if (equal (match-string 1 spec) "-") (- n) n))))

;;;###autoload
(defun am-control-seek (spec)
  "Seek by SPEC: \"+90\", \"-5:00\" (relative) or \"1:23:45\" (absolute)."
  (interactive "sSeek: ")
  (let ((secs (am-control--relative-seconds spec)))
    (or (and secs (am-control--direct 'seek (list "seek" secs "relative")))
        (am-control--media 'seek "music" "seek" spec))))

;;;###autoload
(defun am-control-seek-forward (&optional secs)
  "Seek forward SECS seconds (default 30)."
  (interactive "P")
  (am-control-seek (format "+%d" (or secs 30))))

;;;###autoload
(defun am-control-seek-backward (&optional secs)
  "Seek backward SECS seconds (default 30)."
  (interactive "P")
  (am-control-seek (format "-%d" (or secs 30))))


;;; Status — a read, never a cache to diverge from

(defun am-control-status ()
  "Return the music channel state as a plist, or nil when unreadable.

Keys: :backend :uri :title :chapter :pos-ms :dur-ms :paused :speed :volume
:held.  Every value may be nil — render what you got and poll again.

The pipeline is authoritative: this is derived from the live player, so it
cannot disagree with what the popup shows.  Adapters must not maintain their
own now-playing model alongside it."
  (or (am-control--status-direct)
      (am-control--status-cli)))

(defun am-control--status-direct ()
  "Status straight off the local mpv socket, or nil to fall back.

Reads the same properties `_phone_music_props' batches in cli.py, in one
round-trip, so it agrees with the popup by construction — both are reading
the live player. Skips ~650ms of Python startup, which dominates the CLI
path once the network hop is gone.

`held' still comes from call-guard's flag file rather than from mpv: volume
is call-guard's to own, so the surface reports the hold, it does not infer
it from a volume level."
  (when (and (require 'am-control-mpv nil t) (am-control-mpv-usable-p))
    (when-let* ((p (am-control-mpv-get-properties
                    '("pause" "time-pos" "duration" "speed" "media-title"
                      "chapter-metadata/by-key/title" "volume" "path"))))
      (cl-flet ((get (k) (let ((v (alist-get k p nil nil #'equal)))
                           (if (eq v :json-false) nil v)))
                (ms (v) (and (numberp v) (round (* v 1000)))))
        (let* ((title (get "media-title"))
               (chapter (get "chapter-metadata/by-key/title"))
               ;; Match _mpv_music_label: cache files are named by video id,
               ;; so an unembedded title is a bare `<id>.<ext>` — strip it.
               (title (and (stringp title)
                           (if (and (string-match-p "\\." title)
                                    (not (string-match-p " " title)))
                               (file-name-sans-extension title)
                             title)))
               (label (cond ((and chapter title) (concat chapter " · " title))
                            (t (or chapter title)))))
          (list :backend "phone"
                :uri (get "path")
                :title (and label (not (string-empty-p label)) label)
                :chapter chapter
                :pos-ms (ms (alist-get "time-pos" p nil nil #'equal))
                :dur-ms (ms (alist-get "duration" p nil nil #'equal))
                :paused (eq (alist-get "pause" p nil nil #'equal) t)
                :speed (get "speed")
                :volume (let ((v (get "volume"))) (and (numberp v) (round v)))
                :held (am-control-hold-flag-present-p)))))))

(defun am-control--status-cli ()
  "Status via `media music status --json' — the authoritative slow path."
  (let* ((argv (append (am-control--prefix 'status 'media)
                       (list "music" "status" "--json")))
         (out (am-control--run-sync 'status argv)))
    (when (and out (not (string-empty-p (string-trim out))))
      (condition-case err
          (let* ((json-object-type 'plist)
                 (json-key-type 'keyword)
                 (obj (json-read-from-string (string-trim out))))
            ;; Normalise pos_ms -> :pos-ms so callers read idiomatic elisp.
            (list :backend (plist-get obj :backend)
                  :uri     (plist-get obj :uri)
                  :title   (plist-get obj :title)
                  :chapter (plist-get obj :chapter)
                  :pos-ms  (plist-get obj :pos_ms)
                  :dur-ms  (plist-get obj :dur_ms)
                  :paused  (eq (plist-get obj :paused) t)
                  :speed   (plist-get obj :speed)
                  :volume  (plist-get obj :volume)
                  :held    (eq (plist-get obj :held) t)))
        (error (am-control--log "status parse failed: %S" err) nil)))))

(defun am-control-now-string ()
  "One-line \"what is playing\" summary, for a mode line or a message."
  (let ((s (am-control-status)))
    (cond
     ((null s) "music: unreachable")
     ((null (plist-get s :title)) "music: idle")
     (t (format "%s%s%s"
                (if (plist-get s :paused) "⏸ " "▶ ")
                (plist-get s :title)
                (if (plist-get s :held) " [held]" ""))))))

;;;###autoload
(defun am-control-now ()
  "Show what is playing."
  (interactive)
  (message "%s" (am-control-now-string)))


;;; Adapter loading

(defcustom am-control-adapter nil
  "Active control surface: nil, `empv', `listen', or `emms'.
nil means no front-end is loaded — the CLI and MCP tools remain the only
control surfaces, exactly as before this package existed.  That is a real
state, not a degraded one."
  :type '(choice (const :tag "None (CLI/MCP only)" nil)
                 (const empv) (const listen) (const emms)))

(defvar am-control--loaded-adapter nil
  "Adapter currently set up, so `am-control-setup' can tear it down.")

(defun am-control--adapter-teardown (adapter)
  (when adapter
    (let ((fn (intern (format "am-adapter-%s-teardown" adapter))))
      (when (fboundp fn) (funcall fn)))))

;;;###autoload
(defun am-control-setup ()
  "Load and install the adapter named by `am-control-adapter'.
Tears down a previously loaded adapter first, so switching surfaces is a
`setq' plus this call.  Playback is untouched throughout — the pipeline
never learns that any of this happened."
  (interactive)
  (am-control--adapter-teardown am-control--loaded-adapter)
  (setq am-control--loaded-adapter nil)
  (when-let* ((adapter (or am-control-adapter
                           (let ((env (getenv "MEDIA_CONTROL_SURFACE")))
                             (and env (not (member env '("" "none")))
                                  (intern env))))))
    (require (intern (format "am-adapter-%s" adapter)))
    (let ((fn (intern (format "am-adapter-%s-setup" adapter))))
      (when (fboundp fn) (funcall fn)))
    (setq am-control--loaded-adapter adapter)
    (message "am-control: %s adapter active" adapter)))

(provide 'am-control)
;;; am-control.el ends here
