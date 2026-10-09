package main

import (
	"crypto/sha256"
	"crypto/subtle"
	"strings"
)

// tokenMatches compares digests so secret contents are not compared bytewise.
func tokenMatches(configured, candidate string) bool {
	if configured == "" || candidate == "" {
		return false
	}
	expected := sha256.Sum256([]byte(configured))
	actual := sha256.Sum256([]byte(candidate))
	return subtle.ConstantTimeCompare(expected[:], actual[:]) == 1
}

func bearerValue(header string) string {
	fields := strings.Fields(header)
	if len(fields) != 2 || !strings.EqualFold(fields[0], "Bearer") {
		return ""
	}
	return fields[1]
}
