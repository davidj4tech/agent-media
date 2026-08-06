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

;; Where each action has to happen is `am-control-site's business, not this
;; file's — the same wiring a vanilla init or the Spacemacs layer loads, so
;; the phone cannot drift from them.  Resolved relative to `load-file-name',
;; so a plain `git pull' is enough and no path is hard-coded to a checkout.
(load (expand-file-name
       "../lisp/am-control-site.el"
       (file-name-directory (or load-file-name buffer-file-name default-directory)))
      nil 'nomessage)

;; No adapter: this daemon exists to be scripted (from the agent, a Termux
;; widget, a Tasker action), not to browse.  empv's picker is a keyboard
;; idiom and belongs where there is a keyboard.  Pass `empv' here if that
;; changes.
(am-control-site-setup nil)

(require 'server)
(setq server-name "am-control")

(provide 'am-control-init)
;;; am-control-init.el ends here
