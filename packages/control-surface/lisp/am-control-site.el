;;; am-control-site.el --- Which end of each action this host is  -*- lexical-binding: t; -*-

;;; Commentary:

;; Host wiring, in the repo, once.
;;
;; `am-control' knows *what* the seven actions are; this file knows *where*
;; each one has to happen, which is a fact about how the pipeline is deployed
;; rather than a matter of taste.  It lives here so that a change to where a
;; daemon runs is fixed in the same commit that moves it, instead of drifting
;; out into however many init files happen to drive the surface.
;;
;; Plain Emacs: no Spacemacs, no package manager, no assumptions about your
;; init.  Loading this one file is the whole install —
;;
;;     (load "~/projects/agent-media/packages/control-surface/lisp/am-control-site.el")
;;     (am-control-site-setup 'empv)   ; or nil for the contract with no UI
;;
;; — because it puts its own directory (and `adapters/') on `load-path'
;; relative to `load-file-name'.  Nothing hard-codes a checkout location, so
;; the same call works from any clone, on any host.
;;
;; Keybindings are deliberately NOT here.  The empv adapter installs its own
;; `C-c m' map; anything beyond that is the user's, and belongs in the user's
;; config.

;;; Code:

(defvar am-control-site-dir
  (file-name-directory (or load-file-name buffer-file-name default-directory))
  "The `lisp/' directory of the control-surface package.
Resolved from `load-file-name' so a `git pull' is the entire update path
and no path is hard-coded to a checkout.")

(defvar am-control-site-phone-host "p8ar"
  "Host where `call_guard' runs — the runit service `call-guard'.
Hold is routed here.  See `am-control-site-configure'.")

(defvar am-control-site-hub-host "red5"
  "Host that owns the library, content-type policy, history and state store.
`play' and `queue-add' are routed here.")

(defvar am-control-site-ssh
  '("ssh" "-o" "BatchMode=yes" "-o" "ConnectTimeout=6")
  "ssh prefix for dispatched actions.
BatchMode because the surface commonly runs in a headless daemon: a
password prompt it cannot display would hang the call instead of failing
it, and every action here is fire-and-forget.")

(defun am-control-site-phone-p ()
  "Non-nil when this Emacs is running on the phone (Termux)."
  (file-directory-p "/data/data/com.termux/files"))

(defun am-control-site-media ()
  "Absolute path to the `media' CLI on this host, or nil if it has none.
Absolute on purpose: a systemd- or runit-started daemon inherits a minimal
PATH that need not include ~/.local/bin, so `executable-find' alone finds
nothing even where the CLI is installed."
  (let ((p (expand-file-name "~/.local/bin/media")))
    (or (and (file-executable-p p) p)
        (executable-find "media"))))

(defun am-control-site-load ()
  "Put the control surface on `load-path' and require it."
  (add-to-list 'load-path (directory-file-name am-control-site-dir))
  (add-to-list 'load-path
              (directory-file-name (expand-file-name "adapters" am-control-site-dir)))
  (require 'am-control)
  (require 'am-control-hold))

(defun am-control-site-configure ()
  "Point each action at the host that can actually perform it.

Every branch SETS `am-control-local-actions' outright rather than editing
the list it finds.  Editing it makes this function order-dependent — call it
twice, or after some other config touched the variable, and the host you are
on stops determining the answer."
  (cond
   ;; The phone.  Genuinely local: media, media-call-guard and the mpv they
   ;; talk to are all on this device, over a Unix socket — which is what lets
   ;; am-control-mpv's direct JSON-IPC fast path engage (~2ms vs ~650ms).
   ;; play/queue-add still cross to the hub, which owns the library.
   ((am-control-site-phone-p)
    (setq am-control-local-actions '(toggle next prev seek hold release status)
          am-control-local-command  '("media")
          am-control-remote-command (append am-control-site-ssh
                                            (list am-control-site-hub-host "media"))
          am-control-local-hold-command '("media-call-guard")))

   ;; A host with the CLI (the hub).  Local and remote are the same machine;
   ;; the split still matters because hold is neither — see below.
   ((am-control-site-media)
    (let ((media (am-control-site-media)))
      (setq am-control-local-actions '(toggle next prev seek status)
            am-control-local-command  (list media)
            am-control-remote-command (list media))))

   ;; Anywhere else: no CLI here, so every action crosses to the hub.
   (t
    (setq am-control-local-actions nil
          am-control-local-command  (append am-control-site-ssh
                                            (list am-control-site-hub-host "media"))
          am-control-remote-command (append am-control-site-ssh
                                            (list am-control-site-hub-host "media")))))

  ;; Hold does not follow `media'.  `media-call-guard --hold' does nothing but
  ;; touch a flag file, and the process that POLLS that flag — call_guard —
  ;; runs on the phone, where mpv and the call notifications are.  A hold
  ;; performed anywhere else lands in a state dir no daemon watches and
  ;; silently does nothing.  That is why hold and release are absent from
  ;; `am-control-local-actions' in every off-phone branch above: it routes
  ;; them over ssh AND disables the direct `write-region', which is gated on
  ;; exactly that (see `am-control-hold--direct-p'), so the flag is touched on
  ;; the host that reads it.
  ;;
  ;; Note this is the HOLD prefix pair, not `am-control-remote-command':
  ;; play/queue-add still go to the hub.  The two remotes are different hosts,
  ;; which is the whole reason hold has a prefix pair of its own.
  (unless (am-control-site-phone-p)
    (setq am-control-remote-hold-command
          (append am-control-site-ssh
                  (list am-control-site-phone-host "media-call-guard")))))

;;;###autoload
(defun am-control-site-setup (&optional adapter)
  "Load the control surface, wire it for this host, and install ADAPTER.
ADAPTER is nil (contract only — scriptable, no UI, no keys), or `empv'.
Returns the adapter actually installed."
  (am-control-site-load)
  (am-control-site-configure)
  (setq am-control-adapter adapter)
  (am-control-setup)
  ;; Not taste: without this the status buffer advertises keys that evil has
  ;; shadowed.  Deferred when evil is not up yet, since load order between a
  ;; control surface and an editing mode is nobody's business but Emacs'.
  (require 'am-control-evil)
  (if (featurep 'evil)
      (am-control-evil-setup)
    (with-eval-after-load 'evil (am-control-evil-setup)))
  am-control-adapter)

(provide 'am-control-site)
;;; am-control-site.el ends here
