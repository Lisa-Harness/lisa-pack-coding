// Package client implements the CLI's bounded, authenticated HTTP transport.
package client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Config is copied into a client so commands cannot mutate shared settings.
type Config struct {
	Server, Token string
	Timeout       time.Duration
}

// Client is safe to reuse across commands with the same server and token.
type Client struct {
	base      string
	token     string
	transport *http.Client
}

// APIError exposes the server's machine-readable failure without hiding status.
type APIError struct {
	Status int
	Code   string
}

func (e *APIError) Error() string { return fmt.Sprintf("HTTP %d: %s", e.Status, e.Code) }

// New validates the endpoint before making any network calls.
func New(cfg Config) (*Client, error) {
	parsed, err := url.Parse(cfg.Server)
	if err != nil || parsed.Host == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return nil, fmt.Errorf("server must be an absolute HTTP URL")
	}
	if parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, fmt.Errorf("server URL must not contain credentials, query, or fragment")
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 5 * time.Second
	}
	return &Client{strings.TrimRight(cfg.Server, "/"), cfg.Token, &http.Client{
		Timeout:       cfg.Timeout,
		CheckRedirect: func(r *http.Request, via []*http.Request) error { return http.ErrUseLastResponse },
	}}, nil
}

// Do sends one request; mutations are never retried automatically.
func (c *Client) Do(ctx context.Context, method, path string, body any, target any) error {
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("encode request: %w", err)
		}
		reader = bytes.NewReader(encoded)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, reader)
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	response, err := c.transport.Do(req)
	if err != nil {
		return fmt.Errorf("request %s %s: %w", method, path, err)
	}
	defer response.Body.Close()
	limited := io.LimitReader(response.Body, 2*1024*1024)
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		var failure struct {
			Error string `json:"error"`
		}
		if err := json.NewDecoder(limited).Decode(&failure); err != nil || failure.Error == "" {
			failure.Error = http.StatusText(response.StatusCode)
		}
		return &APIError{response.StatusCode, failure.Error}
	}
	if response.StatusCode == http.StatusNoContent || target == nil {
		return nil
	}
	if err := json.NewDecoder(limited).Decode(target); err != nil {
		return fmt.Errorf("decode response: %w", err)
	}
	return nil
}
