package output

import (
	"bytes"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

type brokenWriter struct{}

func (brokenWriter) Write(p []byte) (int, error) { return 0, errors.New("broken pipe") }

func TestTableColumns(t *testing.T) {
	data := []any{
		map[string]any{"ip": "192.168.1.10", "mac": "aa:bb:cc:dd:ee:01", "hostname": "nas"},
		map[string]any{"ip": "192.168.1.11", "mac": "aa:bb:cc:dd:ee:02", "addresses": []any{"one", "two"}},
	}
	var buffer bytes.Buffer
	if err := Write(&buffer, "table", data); err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(buffer.String(), "\n")
	if !strings.HasPrefix(lines[0], "MAC") || !strings.Contains(lines[0], "ADDRESSES") || !strings.Contains(buffer.String(), "one,two") {
		t.Fatalf("table:\n%s", &buffer)
	}
	first := buffer.String()
	buffer.Reset()
	if err := Write(&buffer, "table", data); err != nil || buffer.String() != first {
		t.Fatal("unstable column order")
	}
}

func TestJSONAndEmptyOutput(t *testing.T) {
	var buffer bytes.Buffer
	value := map[string]any{"hostname": "nas", "ip": "192.168.1.10"}
	if err := Write(&buffer, "json", value); err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(buffer.Bytes(), &decoded); err != nil || decoded["ip"] != value["ip"] {
		t.Fatalf("JSON: %s, %v", &buffer, err)
	}
	buffer.Reset()
	if err := Write(&buffer, "table", []any{}); err != nil || buffer.String() != "(empty)\n" {
		t.Fatal("empty output")
	}
	buffer.Reset()
	if err := Write(&buffer, "table", nil); err != nil || buffer.Len() != 0 {
		t.Fatal("delete output")
	}
}

func TestOutputFailures(t *testing.T) {
	for _, value := range []any{12, []any{12}} {
		if err := Write(&bytes.Buffer{}, "table", value); err == nil {
			t.Fatal("unsupported table input")
		}
	}
	if err := Write(&bytes.Buffer{}, "xml", nil); err == nil {
		t.Fatal("unknown format")
	}
	if err := Write(brokenWriter{}, "json", map[string]any{}); err == nil {
		t.Fatal("write failure lost")
	}
	if err := Write(brokenWriter{}, "table", map[string]any{"mac": "test"}); err == nil {
		t.Fatal("flush failure lost")
	}
	if got := safeCell("a\tb\nc\x1b"); got != "a b c " {
		t.Fatalf("unsafe cell %q", got)
	}
}
