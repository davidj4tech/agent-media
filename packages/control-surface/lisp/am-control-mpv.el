;;; am-control-mpv.el --- Direct mpv JSON-IPC fast path -*- lexical-binding: t; -*-

;;; Commentary:

;; Once the tailnet hop was gone, the bottleneck moved: a `media' invocation
;; costs ~650ms of Python startup on the phone, which dominated everything
;; else.  This file removes that cost for the actions where it is safe, by
;; speaking mpv's JSON-IPC protocol straight from elisp.
;;
;; WHICH ACTIONS ARE SAFE, and why this is not the "Mode B" the design note
;; rejected (docs/control-surface.md §4):
;;
;;   safe    toggle / next / prev / seek — `media music <verb>` does nothing
;;           but call the backend method (verified in cli.py's transport
;;           block: no StateStore write), so going direct is byte-equivalent.
;;           agent-media live-probes mpv rather than trusting its DB, so red5
;;           still sees these on its next poll.
;;
;;   NOT     anything touching VOLUME.  `call_guard' owns music volume during
;;           a duck and restores it afterwards; a front-end writing volume
;;           directly races that restore.  That was the real objection to
;;           Mode B and it still stands.  Ducking therefore does not come
;;           through here — see am-control-hold.el, which touches call-guard's
;;           documented flag file instead.
;;
;;   NOT     play / queue-add / stop — these need the library, content-type
;;           policy and history, and do write state.  They stay on the CLI.
;;
;; Everything degrades: with no endpoint configured, an unreachable socket, or
;; `am-control-prefer-direct' nil, every caller falls back to the `media' CLI
;; and behaviour is unchanged.  red5 has no local music socket, so it simply
;; always takes the CLI path.

;;; Code:

(require 'json)
(require 'subr-x)
(require 'cl-lib)

(defcustom am-control-prefer-direct t
  "When non-nil, use mpv's IPC socket directly for safe transport actions.
Set to nil to force everything through the `media' CLI."
  :type 'boolean
  :group 'am-control)

(defcustom am-control-mpv-endpoint nil
  "mpv IPC endpoint: a Unix socket path, or \"tcp://host:port\".
nil means read `MEDIA_MUSIC_LOCAL_ENDPOINT' from the environment, which is
what agent-media itself uses — so the surface and the pipeline can never
disagree about which player they are talking to."
  :type '(choice (const :tag "From MEDIA_MUSIC_LOCAL_ENDPOINT" nil) string)
  :group 'am-control)

(defcustom am-control-mpv-timeout 0.6
  "Seconds to wait for an mpv reply before falling back to the CLI.
Deliberately short: the socket is local, so a slow reply means something is
wrong and the CLI is the better answer."
  :type 'number
  :group 'am-control)

(defvar am-control-mpv--request-id 0)

(defun am-control-mpv-endpoint ()
  "Configured mpv endpoint, or nil."
  (let ((ep (or am-control-mpv-endpoint
                (getenv "MEDIA_MUSIC_LOCAL_ENDPOINT"))))
    (and ep (not (string-empty-p (string-trim ep))) (string-trim ep))))

(defun am-control-mpv--connect (endpoint)
  "Open a connection to ENDPOINT, or nil on failure."
  (condition-case nil
      (if (string-prefix-p "tcp://" endpoint)
          (let* ((hostport (substring endpoint 6))
                 (colon (string-match-p ":[^:]*\\'" hostport))
                 (host (substring hostport 0 colon))
                 (port (string-to-number (substring hostport (1+ colon)))))
            (make-network-process :name "am-mpv" :host host :service port
                                  :coding 'utf-8 :noquery t))
        (make-network-process :name "am-mpv" :family 'local
                              :service (expand-file-name endpoint)
                              :coding 'utf-8 :noquery t))
    (error nil)))

(defun am-control-mpv--converse (commands)
  "Send COMMANDS (a list of arg-lists) over one connection.
Return a list of results in order, or nil if the connection failed.

One connection for N commands is the point: reconnecting per property is
what makes a naive client slow, and it is why agent-media batches its own
reads too."
  (when-let* ((endpoint (am-control-mpv-endpoint))
              (proc (am-control-mpv--connect endpoint)))
    (unwind-protect
        (let ((pending nil) (acc "") (results (make-hash-table :test #'eql)))
          (set-process-filter proc (lambda (_p s) (setq acc (concat acc s))))
          (dolist (cmd commands)
            (setq am-control-mpv--request-id (1+ am-control-mpv--request-id))
            (push am-control-mpv--request-id pending)
            (process-send-string
             proc (concat (json-encode `(("command" . ,(vconcat cmd))
                                         ("request_id" . ,am-control-mpv--request-id)))
                          "\n")))
          (setq pending (nreverse pending))
          (let ((deadline (+ (float-time) am-control-mpv-timeout)))
            (while (and (< (hash-table-count results) (length pending))
                        (< (float-time) deadline)
                        (process-live-p proc))
              (accept-process-output proc 0.05)
              ;; Consume whole lines; mpv interleaves async events with
              ;; replies on the same socket, so match on request_id rather
              ;; than assuming the next line is ours.
              (while (string-match "\\`\\([^\n]*\\)\n" acc)
                (let ((line (match-string 1 acc)))
                  (setq acc (substring acc (match-end 0)))
                  (condition-case nil
                      (let* ((obj (let ((json-object-type 'alist))
                                    (json-read-from-string line)))
                             (rid (alist-get 'request_id obj)))
                        (when (and rid (memq rid pending))
                          (puthash rid
                                   (and (equal (alist-get 'error obj) "success")
                                        (alist-get 'data obj))
                                   results)))
                    (error nil))))))
          (and (= (hash-table-count results) (length pending))
               (mapcar (lambda (rid) (gethash rid results)) pending)))
      (ignore-errors (delete-process proc)))))

(defun am-control-mpv-command (&rest args)
  "Send a single mpv command.  Return (t . DATA) on success, nil otherwise.
Wrapped in a cons so a successful nil `data' (the common case for transport
commands) is distinguishable from a failed call."
  (when am-control-prefer-direct
    (when-let* ((res (am-control-mpv--converse (list args))))
      (cons t (car res)))))

(defun am-control-mpv-get-properties (props)
  "Read PROPS (a list of property-name strings) in one round-trip.
Return an alist of (NAME . VALUE), or nil if the socket was unreachable."
  (when am-control-prefer-direct
    (when-let* ((res (am-control-mpv--converse
                      (mapcar (lambda (p) (list "get_property" p)) props))))
      (cl-mapcar #'cons props res))))

(defun am-control-mpv-local-p ()
  "Non-nil when the endpoint is a genuinely local Unix socket.

The fast path is deliberately restricted to Unix sockets.  A `tcp://'
endpoint is the bridge red5 uses to reach the phone: going direct there
would still cross the tailnet, and the liveness check below would cost a
second round-trip to decide — likely slower than the CLI it replaced, for no
gain.  On red5 this returns nil and everything takes the CLI path, exactly
as before."
  (when-let* ((ep (am-control-mpv-endpoint)))
    (not (string-prefix-p "tcp://" ep))))

(defvar am-control-mpv--live-cache nil
  "Cons of (TIMESTAMP . LIVE-P), so a burst of keypresses checks once.")

(defcustom am-control-mpv-live-ttl 2.0
  "Seconds to trust a cached liveness answer.
Short enough that starting or stopping playback is noticed promptly, long
enough that holding down a transport key doesn't re-probe every event."
  :type 'number
  :group 'am-control)

(defun am-control-mpv-live-p (&optional force)
  "Non-nil when the local mpv has a track loaded.

Mirrors agent-media's own live-backend rule (`_music_live_backend' in
cli.py): an idle mpv means the pipeline would read Mopidy instead, so the
fast path must decline and let the CLI decide which backend to drive."
  (if (and (not force) am-control-mpv--live-cache
           (< (- (float-time) (car am-control-mpv--live-cache))
              am-control-mpv-live-ttl))
      (cdr am-control-mpv--live-cache)
    (let* ((props (am-control-mpv-get-properties '("idle-active")))
           (live (and props
                      (eq (alist-get "idle-active" props nil nil #'equal)
                          :json-false))))
      (setq am-control-mpv--live-cache (cons (float-time) live))
      live)))

(defun am-control-mpv-usable-p ()
  "Non-nil when the direct path may be used for a transport action."
  (and am-control-prefer-direct
       (am-control-mpv-local-p)
       (am-control-mpv-live-p)))

(defun am-control-mpv-invalidate ()
  "Drop the cached liveness answer.
Called after actions that can change whether a track is loaded."
  (setq am-control-mpv--live-cache nil))

(provide 'am-control-mpv)
;;; am-control-mpv.el ends here
