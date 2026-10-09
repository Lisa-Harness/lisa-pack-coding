package main

import "net/http"

func bearerAuth(token string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !tokenMatches(token, bearerValue(r.Header.Get("Authorization"))) {
			w.Header().Set("WWW-Authenticate", `Bearer realm="leasewarden"`)
			writeError(w, http.StatusUnauthorized, "unauthorized")
			return
		}
		next.ServeHTTP(w, r)
	})
}
