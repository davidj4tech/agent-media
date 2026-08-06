;;; am-control-init.el --- Minimal init for the phone control daemon -*- lexical-binding: t; -*-

;;; Commentary:

;; Init for the phone's dedicated agent-media control daemon.  Loaded with
;; `emacs -Q', so the phone's own Emacs config (and any Spacemacs) is not
;; involved: this daemon is a remote control, not an editor.  Keeping it -Q
;; is the point — it starts fast, stays small, and cannot be broken by an
;; unrelated config change.
;;
;; It runs a NAMED server ("am-control"), so it never collides with the
;; phone's generic `emacsd' service or its default server socket:
;;
;;     emacsclient -s am-control -e '(am-control-toggle)'
;;
;; Why a daemon on the phone at all: this is where fetch, mpv and call_guard
;; already live, so control here skips the ~0.8s tailnet round-trip to red5.
;; That is the same saving as the standing barge-in TODO.  See
;; docs/control-surface.md §6.

;;; Code:

(setq inhibit-startup-screen t
      make-backup-files nil
      auto-save-default nil
      create-lockfiles nil)

;; This file lives at <repo>/packages/control-surface/phone/, so the lisp
;; directories are siblings.  Resolving relative to `load-file-name' means a
;; plain `git pull' is enough — no path is hard-coded to a checkout location.
(let* ((here (file-name-directory (or load-file-name buffer-file-name
                                      default-directory)))
       (root (expand-file-name ".." here)))
  (add-to-list 'load-path (expand-file-name "lisp" root))
  (add-to-list 'load-path (expand-file-name "lisp/adapters" root)))

(require 'am-control)
(require 'am-control-hold)

;; Per-action dispatch (docs/control-surface.md §6.3).  On the phone the
;; local half is genuinely local: `media' and `media-call-guard' are both on
;; PATH here, and the mpv they talk to is on the same device — a Unix socket,
;; not the tcp bridge red5 uses.  play/queue-add still go to red5, which owns
;; the library, content-type policy and history.
(setq am-control-local-command  '("media")
      am-control-remote-command '("ssh" "-o" "BatchMode=yes"
                                  "-o" "ConnectTimeout=6" "red5" "media")
      am-control-local-hold-command '("media-call-guard")
      am-control-remote-hold-command
      '("ssh" "-o" "BatchMode=yes" "-o" "ConnectTimeout=6" "red5"
        "/home/ryer/projects/agent-media/.venv/bin/media-call-guard"))

;; No adapter: this daemon exists to be scripted (from the agent, a Termux
;; widget, a Tasker action), not to browse.  empv's picker is a keyboard
;; idiom and belongs on red5.  Set `am-control-adapter' here if that changes.
(setq am-control-adapter nil)

(require 'server)
(setq server-name "am-control")

(provide 'am-control-init)
;;; am-control-init.el ends here
