;;; am-control-hold.el --- Duck-and-hold for a voice chat -*- lexical-binding: t; -*-

;;; Commentary:

;; The duck action, and the reason this package is worth building.
;;
;; `call_guard' already implements exactly the semantics wanted, and it is not
;; MPD-shaped — it went socket-shaped some time ago.  The external-hold flag
;; file is a documented, supported, idempotent trigger: any external actor may
;; set it, and the guard debounces (engage/release windows), pauses speech,
;; DUCKS music rather than pausing it, and auto-resumes on release.
;;
;; So there is no new mechanism here.  `hold' is `media-call-guard --hold' and
;; `release' is `--release'.  What this file adds is the thing a shell call
;; cannot give you: an `unwind-protect' around the pair, so a C-g or an error
;; during a voice chat cannot leave music stuck quiet.  That is the clean
;; replacement for an MPD-style `mpc pause … mpc play'.
;;
;; Preserve the asymmetry: HOLD IS NOT PAUSE.  Music ducks, speech pauses.
;; That split is the pipeline's policy (route/policy.py, route/coordinator.py).
;; An adapter that "helpfully" pauses music on hold has broken the contract.

;;; Code:

(require 'am-control)

(defvar am-control-hold--depth 0
  "Nesting depth of active holds.
The guard is idempotent, but tracking depth keeps a nested
`am-control-with-hold' from releasing early on the inner exit.")

;;;###autoload
(defun am-control-hold ()
  "Duck the music channel and hold it down.
Idempotent: holding twice is one hold.  Speech pauses, music ducks —
call-guard decides which, not us."
  (interactive)
  (setq am-control-hold--depth (1+ am-control-hold--depth))
  (when (= am-control-hold--depth 1)
    (am-control--run 'hold (append (am-control--prefix 'hold 'hold)
                                   (list "--hold")))))

;;;###autoload
(defun am-control-release ()
  "Release the hold.  Music returns to its previous level automatically.
Un-ducking never starts playback, so this is always safe to call."
  (interactive)
  (setq am-control-hold--depth (max 0 (1- am-control-hold--depth)))
  (when (zerop am-control-hold--depth)
    (am-control--run 'release (append (am-control--prefix 'release 'hold)
                                      (list "--release")))))

;;;###autoload
(defun am-control-hold-toggle ()
  "Toggle the hold.  Reads live state so it agrees with an external trigger."
  (interactive)
  (if (or (> am-control-hold--depth 0)
          (plist-get (am-control-status) :held))
      (progn (setq am-control-hold--depth 1) (am-control-release))
    (am-control-hold)))

;;;###autoload
(defmacro am-control-with-hold (&rest body)
  "Run BODY with the music channel ducked, releasing on any exit.
The release sits in an `unwind-protect', so C-g or an error cannot strand
the hold.  call-guard's own release debounce means rapid hold/release
flicker at utterance boundaries will not thrash the volume."
  (declare (indent 0) (debug t))
  `(progn
     (am-control-hold)
     (unwind-protect (progn ,@body)
       (am-control-release))))

;;;###autoload
(defun am-control-hold-reset ()
  "Force the hold released and zero the depth counter.
For the case where Emacs lost track — e.g. a crash mid-hold left the
counter non-zero while the flag file is already gone."
  (interactive)
  (setq am-control-hold--depth 0)
  (am-control--run 'release (append (am-control--prefix 'release 'hold)
                                    (list "--release"))))

(provide 'am-control-hold)
;;; am-control-hold.el ends here
