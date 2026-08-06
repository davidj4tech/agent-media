;;; config.el --- am-control layer  -*- lexical-binding: t; -*-

;; A Spacemacs layer for agent-media's music control surface.
;;
;; Add this directory's parent to `dotspacemacs-configuration-layer-path' and
;; `am-control' to `dotspacemacs-configuration-layers':
;;
;;   dotspacemacs-configuration-layer-path
;;     '("~/.spacemacs.d/private/"
;;       "~/projects/agent-media/packages/control-surface/spacemacs/")
;;
;; Everything Spacemacs-specific is in this layer and nowhere else: the host
;; dispatch is `am-control-site', which is plain Emacs and is what a vanilla
;; init loads directly.  The layer is a thin caller, which is the point — it
;; can go stale without the surface going stale.

(let ((site (expand-file-name
             "../../lisp/am-control-site.el"
             (file-name-directory (or load-file-name buffer-file-name)))))
  (if (not (file-readable-p site))
      ;; The layer path points at a checkout; if the checkout moved, say so
      ;; rather than throwing during startup.  A dead front-end must never
      ;; take Emacs down with it, and it must never take the CLI down either
      ;; — the pipeline does not know this layer exists.
      (message "am-control: no control surface at %s — layer inert" site)
    (load site nil 'nomessage)
    (am-control-site-setup 'empv)))
