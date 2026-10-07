// Package lsp — языковой сервер Gasem (Language Server Protocol): ошибки
// прямо в редакторе, переход к объявлению, подсказки при наведении,
// автодополнение, структура файла и выравнивание (gasem fmt).
package lsp

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net/textproto"
	"strconv"
	"strings"
	"sync"
)

// message — запрос, уведомление или ответ JSON-RPC 2.0.
type message struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method,omitempty"`
	Params  json.RawMessage `json:"params,omitempty"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

const (
	codeParseError     = -32700
	codeMethodNotFound = -32601
	codeInternalError  = -32603
)

// conn читает и пишет сообщения с заголовком Content-Length.
type conn struct {
	in  *bufio.Reader
	mu  sync.Mutex
	out io.Writer
}

func newConn(r io.Reader, w io.Writer) *conn {
	return &conn{in: bufio.NewReaderSize(r, 1<<16), out: w}
}

func (c *conn) read() (*message, error) {
	tp := textproto.NewReader(c.in)
	header, err := tp.ReadMIMEHeader()
	if err != nil {
		return nil, err
	}
	n, err := strconv.Atoi(strings.TrimSpace(header.Get("Content-Length")))
	if err != nil || n < 0 {
		return nil, fmt.Errorf("неверный заголовок Content-Length: %q", header.Get("Content-Length"))
	}
	body := make([]byte, n)
	if _, err := io.ReadFull(c.in, body); err != nil {
		return nil, err
	}
	var m message
	if err := json.Unmarshal(body, &m); err != nil {
		return &message{Method: "", Error: &rpcError{codeParseError, err.Error()}}, nil
	}
	return &m, nil
}

func (c *conn) write(m *message) error {
	m.JSONRPC = "2.0"
	body, err := json.Marshal(m)
	if err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, err := fmt.Fprintf(c.out, "Content-Length: %d\r\n\r\n", len(body)); err != nil {
		return err
	}
	_, err = c.out.Write(body)
	return err
}

func (c *conn) reply(id json.RawMessage, result any) error {
	data, err := json.Marshal(result)
	if err != nil {
		return err
	}
	return c.write(&message{ID: id, Result: data})
}

func (c *conn) replyError(id json.RawMessage, code int, msg string) error {
	return c.write(&message{ID: id, Error: &rpcError{code, msg}})
}

func (c *conn) notify(method string, params any) error {
	data, err := json.Marshal(params)
	if err != nil {
		return err
	}
	return c.write(&message{Method: method, Params: data})
}
