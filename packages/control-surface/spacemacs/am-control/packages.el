;;; packages.el --- am-control layer  -*- lexical-binding: t; -*-

;; The control surface is not a package.  It is loaded from this checkout by
;; `config.el' (via `am-control-site'), so a `git pull' here is the whole
;; update path and there is no second copy to drift.
;;
;; Only empv comes from MELPA, and only for the part that is genuinely UI: its
;; YouTube search and completing-read machinery.  The adapter routes the
;; *selected candidate* to the contract and never calls empv's own playback
;; functions, so no mpv is ever started inside Emacs.  `M-x
;; am-adapter-empv-check' confirms that.
;;
;; empv is a soft dependency: the adapter only requires it when you actually
;; open the picker, so every transport binding works with empv absent.

(defconst am-control-packages '(empv))

(defun am-control/init-empv ()
  (use-package empv
    :defer t
    :init
    ;; empv's search needs an Invidious instance (nil by default, which makes
    ;; the picker error out rather than fail quietly).  Public instances rot;
    ;; override `empv-invidious-instance' in your own config when this one
    ;; stops answering, and pick a live one from https://api.invidious.io.
    ;; `am-empv-play-url' — paste a URL — never needs an instance at all.
    (unless (bound-and-true-p empv-invidious-instance)
      (setq empv-invidious-instance "https://yewtu.be/api/v1"))))
