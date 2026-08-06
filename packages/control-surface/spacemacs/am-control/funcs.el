;;; funcs.el --- am-control layer  -*- lexical-binding: t; -*-

;; The bindings are offered, not imposed: which leader prefix is free is a
;; property of *your* config, not of the pipeline, so the layer ships the map
;; as a function and you choose where it lands.
;;
;;     (am-control/bind-leader "am")   ; SPC a m — Spacemacs' stock, empty
;;                                     ; "music" prefix

(defun am-control/bind-leader (prefix)
  "Bind the control surface under leader PREFIX (e.g. \"am\" for `SPC a m').
The adapter's own `C-c m' map is installed separately by `am-control-setup'
and is unaffected — this is the same commands, reachable the Spacemacs way
as well."
  (spacemacs/declare-prefix prefix "music")
  (apply #'spacemacs/set-leader-keys
         (cl-loop for (key . cmd)
                  in '(("m" . am-control-toggle)
                       ("n" . am-control-next)
                       ("p" . am-control-prev)
                       ("f" . am-control-seek-forward)
                       ("b" . am-control-seek-backward)
                       ("u" . am-empv-play-url)     ; paste a URL — no empv config needed
                       ("s" . am-empv-search)       ; empv's picker -> play
                       ("a" . am-empv-search-queue) ; empv's picker -> queue
                       ("h" . am-control-hold-toggle) ; DUCK, not pause
                       ("i" . am-control-now)
                       ("q" . am-empv-queue))
                  append (list (concat prefix key) cmd))))
