;;; am-adapter-empv.el --- empv.el as an agent-media control surface -*- lexical-binding: t; -*-

;;; Commentary:

;; empv is not a UI, it is a player: left alone it starts and owns an mpv.
;; This adapter uses empv for the part that is genuinely UI — its YouTube
;; search and its completing-read machinery — and routes the *selected*
;; candidate to the contract in `am-control.el'.  empv's own playback
;; functions (`empv-play', `empv-play-or-enqueue', `empv-start') are never
;; called, so no mpv is ever created here.
;;
;; That is the checkable invariant for review: after loading this adapter and
;; using it, no mpv process exists that the adapter created.  `M-x
;; am-adapter-empv-check' reports on it.
;;
;; Not done, though it would now be permitted: setting `empv-mpv-binary' to
;; stop empv spawning mpv at all.  It simply isn't needed — this adapter never
;; calls empv's playback functions, so there is nothing to neuter, and leaving
;; empv's own globals alone means `M-x empv-play' still behaves like empv for
;; anyone who wants it.  That is empv being empv, outside the contract.
;;
;; (An earlier draft refused this on reversibility grounds.  The spec is now
;; independence, not reversibility — see docs/control-surface.md §8 — so the
;; choice is just local taste, not a rule.)
;;
;; Prerequisite: empv's search needs `empv-invidious-instance' set (it is nil
;; by default).  Without it, use `am-empv-play-url', which needs nothing.

;;; Code:

(require 'am-control)
(require 'am-control-hold)

;; empv is loaded lazily (it is optional), so tell the byte compiler these
;; exist rather than letting it warn on every build.
(declare-function empv--youtube-search "empv" (term type page callback))
(declare-function empv--completing-read-object "empv" (prompt objects &rest args))
(declare-function empv--format-yt-item "empv" (it))
(declare-function empv--youtube-item-extract-link "empv" (item))
(defvar empv-invidious-instance)
(defvar empv--process)

(defvar am-adapter-empv--available nil)

(defgroup am-adapter-empv nil
  "empv-backed control surface for agent-media."
  :group 'am-control
  :prefix "am-empv-")

(defcustom am-empv-prefix-key "C-c m"
  "Prefix key for the empv control surface."
  :type 'string)

(defcustom am-empv-seek-step 30
  "Seconds for the seek-forward/backward bindings."
  :type 'integer)


;;; Picking — empv's UI, our dispatch

(defun am-adapter-empv--require ()
  (unless am-adapter-empv--available
    (setq am-adapter-empv--available (require 'empv nil t)))
  (unless am-adapter-empv--available
    (user-error "am-control: empv is not installed"))
  (unless (and (boundp 'empv-invidious-instance) empv-invidious-instance)
    (user-error
     "am-control: set `empv-invidious-instance' for search, or use `am-empv-play-url'")))

(defun am-adapter-empv--pick (term type action)
  "Search TERM of TYPE with empv, then apply ACTION to the chosen link.
ACTION is a function of one argument, the URL.  Nothing is played by empv;
the selection is handed straight to the contract."
  (am-adapter-empv--require)
  (empv--youtube-search
   term type 1
   (lambda (results)
     (if (not results)
         (message "am-control: no results for %s" term)
       (let ((selected (empv--completing-read-object
                        "YouTube results"
                        results
                        :formatter #'empv--format-yt-item
                        :category 'empv-youtube-item
                        :sort? nil)))
         (when selected
           (funcall action (empv--youtube-item-extract-link selected))))))))

;;;###autoload
(defun am-empv-search (term)
  "Search YouTube with empv's picker and play the choice on the music channel."
  (interactive (list (read-string "Play from YouTube: ")))
  (am-adapter-empv--pick
   term 'video
   (lambda (url)
     (message "am-control: playing %s" url)
     (am-control-play url))))

;;;###autoload
(defun am-empv-search-queue (term)
  "Search YouTube with empv's picker and queue the choice."
  (interactive (list (read-string "Queue from YouTube: ")))
  (am-adapter-empv--pick
   term 'video
   (lambda (url)
     (message "am-control: queued %s" url)
     (am-control-queue-add url))))

;;;###autoload
(defun am-empv-play-url (url)
  "Play URL directly.  Needs no empv configuration at all."
  (interactive "sURL: ")
  (am-control-play url))


;;; Status buffer

(defun am-adapter-empv--status-lines ()
  (let ((s (am-control-status)))
    (if (null s)
        (list "music channel unreachable")
      (list
       (format "  %-9s %s" "state"
               (cond ((null (plist-get s :title)) "idle")
                     ((plist-get s :paused) "paused")
                     (t "playing")))
       (format "  %-9s %s" "title"   (or (plist-get s :title) "—"))
       (format "  %-9s %s" "chapter" (or (plist-get s :chapter) "—"))
       (format "  %-9s %s" "backend" (or (plist-get s :backend) "—"))
       (format "  %-9s %s / %s" "position"
               (am-adapter-empv--fmt-ms (plist-get s :pos-ms))
               (am-adapter-empv--fmt-ms (plist-get s :dur-ms)))
       (format "  %-9s %s" "volume"  (or (plist-get s :volume) "—"))
       (format "  %-9s %s" "held"    (if (plist-get s :held) "yes (ducked)" "no"))
       (format "  %-9s %s" "uri"     (or (plist-get s :uri) "—"))))))

(defun am-adapter-empv--fmt-ms (ms)
  (if (not (numberp ms)) "—"
    (let* ((total (/ ms 1000)) (h (/ total 3600))
           (m (/ (% total 3600) 60)) (s (% total 60)))
      (if (> h 0) (format "%d:%02d:%02d" h m s) (format "%d:%02d" m s)))))

;;;###autoload
(defun am-empv-queue ()
  "Show the music channel's live state.

Note this is the *pipeline's* state, not an empv queue: empv's queue belongs
to empv's own mpv, which this adapter never starts.  The pipeline is the only
source of truth, so what you see here is what is actually audible."
  (interactive)
  (let ((buf (get-buffer-create "*am-control: music*")))
    (with-current-buffer buf
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert "agent-media — music channel\n\n")
        (insert (string-join (am-adapter-empv--status-lines) "\n"))
        (insert "\n\n  g refresh   SPC toggle   n/p next/prev   h hold   q bury\n"))
      (goto-char (point-min))
      (am-adapter-empv-status-mode))
    (display-buffer buf)))

(defvar am-adapter-empv-status-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "g")   #'am-empv-queue)
    (define-key map (kbd "SPC") #'am-control-toggle)
    (define-key map (kbd "n")   #'am-control-next)
    (define-key map (kbd "p")   #'am-control-prev)
    (define-key map (kbd "h")   #'am-control-hold-toggle)
    (define-key map (kbd "q")   #'bury-buffer)
    map))

(define-derived-mode am-adapter-empv-status-mode special-mode "am-music"
  "Major mode for the agent-media music status buffer.")


;;; Keymap — installed by setup, removed by teardown

(defvar am-adapter-empv-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "SPC") #'am-control-toggle)
    (define-key map (kbd "n")   #'am-control-next)
    (define-key map (kbd "p")   #'am-control-prev)
    (define-key map (kbd "s")   #'am-empv-search)
    (define-key map (kbd "a")   #'am-empv-search-queue)
    (define-key map (kbd "u")   #'am-empv-play-url)
    (define-key map (kbd "f")   #'am-control-seek-forward)
    (define-key map (kbd "b")   #'am-control-seek-backward)
    (define-key map (kbd "h")   #'am-control-hold-toggle)
    (define-key map (kbd "q")   #'am-empv-queue)
    (define-key map (kbd "?")   #'am-control-now)
    map)
  "Keymap for the empv control surface.")

;;;###autoload
(defun am-adapter-empv-setup ()
  "Install the empv control surface."
  (global-set-key (kbd am-empv-prefix-key) am-adapter-empv-map))

(defun am-adapter-empv-teardown ()
  "Remove the empv control surface, leaving empv itself untouched."
  (global-unset-key (kbd am-empv-prefix-key)))

;;;###autoload
(defun am-adapter-empv-check ()
  "Report whether empv currently owns an mpv process.
The adapter never starts one; a live process means empv's own commands were
used directly, outside the contract."
  (interactive)
  (let ((live (and (boundp 'empv--process)
                   (process-live-p (symbol-value 'empv--process)))))
    (message "am-control: empv mpv process %s"
             (if live "IS live (started by empv itself, not by this adapter)"
               "is not running — contract intact"))))

(provide 'am-adapter-empv)
;;; am-adapter-empv.el ends here
