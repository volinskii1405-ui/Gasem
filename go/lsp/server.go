package lsp

import (
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf16"
	"unicode/utf8"

	"github.com/volinskii1405-ui/Gasem/go/gasem"
)

// Server — языковой сервер: держит открытые документы и последние результаты разбора.
type Server struct {
	conn *conn

	mu        sync.Mutex
	workspace string            // папка проекта (может быть пустой)
	docs      map[string]string // открытые документы: путь → текст
	results   map[string]*gasem.Analysis
	good      map[string]*gasem.Analysis // последний разбор без ошибок (для значений символов)
	published map[string]bool            // файлы, для которых опубликованы непустые диагностики
	timer     *time.Timer
	dirty     map[string]bool // документы, изменённые после последней проверки
	shutdown  bool
	debounce  time.Duration
}

// Serve запускает сервер на r/w (обычно stdin/stdout) до команды exit.
func Serve(r io.Reader, w io.Writer) error {
	s := &Server{
		conn:      newConn(r, w),
		docs:      map[string]string{},
		results:   map[string]*gasem.Analysis{},
		good:      map[string]*gasem.Analysis{},
		published: map[string]bool{},
		dirty:     map[string]bool{},
		debounce:  150 * time.Millisecond,
	}
	return s.run()
}

func (s *Server) run() error {
	for {
		m, err := s.conn.read()
		if err != nil {
			if err == io.EOF {
				return nil
			}
			return err
		}
		if m.Method == "exit" {
			return nil
		}
		s.handle(m)
	}
}

func (s *Server) handle(m *message) {
	isRequest := len(m.ID) > 0
	defer func() {
		if r := recover(); r != nil && isRequest {
			_ = s.conn.replyError(m.ID, codeInternalError, fmt.Sprint(r))
		}
	}()
	if m.Error != nil && m.Method == "" {
		return // ответ клиента или неразобранное сообщение
	}
	result, handled := s.dispatch(m)
	if !isRequest {
		return
	}
	if !handled {
		_ = s.conn.replyError(m.ID, codeMethodNotFound, "метод не поддерживается: "+m.Method)
		return
	}
	_ = s.conn.reply(m.ID, result)
}

func (s *Server) dispatch(m *message) (any, bool) {
	switch m.Method {
	case "initialize":
		return s.initialize(m.Params), true
	case "initialized", "$/cancelRequest", "$/setTrace", "workspace/didChangeConfiguration",
		"workspace/didChangeWatchedFiles":
		return nil, true
	case "shutdown":
		s.shutdown = true
		return nil, true
	case "textDocument/didOpen":
		var p struct {
			TextDocument struct {
				URI  string `json:"uri"`
				Text string `json:"text"`
			} `json:"textDocument"`
		}
		_ = json.Unmarshal(m.Params, &p)
		s.change(uriToPath(p.TextDocument.URI), p.TextDocument.Text, true)
		return nil, true
	case "textDocument/didChange":
		var p struct {
			TextDocument struct {
				URI string `json:"uri"`
			} `json:"textDocument"`
			ContentChanges []struct {
				Text string `json:"text"`
			} `json:"contentChanges"`
		}
		_ = json.Unmarshal(m.Params, &p)
		if n := len(p.ContentChanges); n > 0 {
			s.change(uriToPath(p.TextDocument.URI), p.ContentChanges[n-1].Text, false)
		}
		return nil, true
	case "textDocument/didSave":
		var p docParams
		_ = json.Unmarshal(m.Params, &p)
		s.mu.Lock()
		s.dirty[uriToPath(p.TextDocument.URI)] = true
		s.mu.Unlock()
		s.schedule(0)
		return nil, true
	case "textDocument/didClose":
		var p docParams
		_ = json.Unmarshal(m.Params, &p)
		s.mu.Lock()
		delete(s.docs, uriToPath(p.TextDocument.URI))
		s.mu.Unlock()
		return nil, true
	case "textDocument/definition":
		var p posParams
		_ = json.Unmarshal(m.Params, &p)
		return s.definition(p), true
	case "textDocument/hover":
		var p posParams
		_ = json.Unmarshal(m.Params, &p)
		return s.hover(p), true
	case "textDocument/completion":
		var p posParams
		_ = json.Unmarshal(m.Params, &p)
		return s.completion(p), true
	case "textDocument/documentSymbol":
		var p docParams
		_ = json.Unmarshal(m.Params, &p)
		return s.documentSymbols(uriToPath(p.TextDocument.URI)), true
	case "textDocument/references":
		var p posParams
		_ = json.Unmarshal(m.Params, &p)
		return s.references(p), true
	case "textDocument/formatting":
		var p docParams
		_ = json.Unmarshal(m.Params, &p)
		return s.formatting(uriToPath(p.TextDocument.URI)), true
	}
	return nil, false
}

type docParams struct {
	TextDocument struct {
		URI string `json:"uri"`
	} `json:"textDocument"`
}

type posParams struct {
	TextDocument struct {
		URI string `json:"uri"`
	} `json:"textDocument"`
	Position Position `json:"position"`
}

// Position — строка и столбец в UTF-16, как в LSP.
type Position struct {
	Line      int `json:"line"`
	Character int `json:"character"`
}

type Range struct {
	Start Position `json:"start"`
	End   Position `json:"end"`
}

type Location struct {
	URI   string `json:"uri"`
	Range Range  `json:"range"`
}

func (s *Server) initialize(params json.RawMessage) any {
	var p struct {
		RootURI          string `json:"rootUri"`
		RootPath         string `json:"rootPath"`
		WorkspaceFolders []struct {
			URI string `json:"uri"`
		} `json:"workspaceFolders"`
	}
	_ = json.Unmarshal(params, &p)
	s.mu.Lock()
	switch {
	case len(p.WorkspaceFolders) > 0:
		s.workspace = uriToPath(p.WorkspaceFolders[0].URI)
	case p.RootURI != "":
		s.workspace = uriToPath(p.RootURI)
	default:
		s.workspace = p.RootPath
	}
	s.mu.Unlock()
	return map[string]any{
		"capabilities": map[string]any{
			"textDocumentSync": map[string]any{"openClose": true, "change": 1,
				"save": map[string]any{"includeText": false}},
			"definitionProvider":         true,
			"hoverProvider":              true,
			"referencesProvider":         true,
			"documentSymbolProvider":     true,
			"documentFormattingProvider": true,
			"completionProvider":         map[string]any{"triggerCharacters": []string{"."}},
		},
		"serverInfo": map[string]any{"name": "gasem", "version": gasem.Version},
	}
}

// ---------------------------------------------------------------- пути и URI

func uriToPath(uri string) string {
	u, err := url.Parse(uri)
	if err != nil || u.Scheme != "file" {
		return uri
	}
	p := u.Path
	if runtime.GOOS == "windows" {
		p = strings.TrimPrefix(p, "/")
		p = filepath.FromSlash(p)
	}
	return normPath(p)
}

func pathToURI(path string) string {
	p := filepath.ToSlash(path)
	if len(p) >= 2 && p[1] == ':' { // Windows: как в VS Code — file:///c%3A/папка/файл.gsm
		parts := strings.Split(strings.ReplaceAll(p[2:], `\`, "/"), "/")
		for i, part := range parts {
			parts[i] = url.PathEscape(part)
		}
		return "file:///" + strings.ToLower(p[:1]) + "%3A" + strings.Join(parts, "/")
	}
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	return (&url.URL{Scheme: "file", Path: p}).String()
}

func normPath(path string) string {
	if a, err := filepath.Abs(path); err == nil {
		return filepath.Clean(a)
	}
	return filepath.Clean(path)
}

// ---------------------------------------------------------------- документы

// text — текст файла: из редактора, если он открыт, иначе с диска.
func (s *Server) text(path string) (string, bool) {
	s.mu.Lock()
	t, ok := s.docs[path]
	s.mu.Unlock()
	if ok {
		return t, true
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return "", false
	}
	return strings.TrimPrefix(string(data), "\ufeff"), true
}

func (s *Server) readFile(path string) ([]byte, error) {
	s.mu.Lock()
	t, ok := s.docs[normPath(path)]
	s.mu.Unlock()
	if ok {
		return []byte(t), nil
	}
	return os.ReadFile(path)
}

func (s *Server) change(path, text string, open bool) {
	s.mu.Lock()
	s.docs[path] = text
	s.dirty[path] = true
	s.mu.Unlock()
	if open {
		s.schedule(0)
	} else {
		s.schedule(s.debounce)
	}
}

// schedule запускает проверку изменённых документов через delay (повторные изменения откладывают её).
func (s *Server) schedule(delay time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.timer != nil {
		s.timer.Stop()
	}
	if delay == 0 {
		go s.checkDirty()
		return
	}
	s.timer = time.AfterFunc(delay, s.checkDirty)
}

var checkMu sync.Mutex

func (s *Server) checkDirty() {
	checkMu.Lock()
	defer checkMu.Unlock()
	s.mu.Lock()
	var paths []string
	for p := range s.dirty {
		paths = append(paths, p)
	}
	s.dirty = map[string]bool{}
	s.mu.Unlock()
	sort.Strings(paths)
	done := map[string]bool{}
	for _, p := range paths {
		root := s.rootFor(p)
		if done[root] {
			continue
		}
		done[root] = true
		s.publish(root, s.analyze(root))
	}
}

// analyze разбирает программу, начиная с root, и запоминает результат.
func (s *Server) analyze(root string) *gasem.Analysis {
	a := gasem.Analyze(root, s.readFile)
	s.mu.Lock()
	s.results[root] = a
	if a.Result != nil {
		s.good[root] = a
	}
	s.mu.Unlock()
	return a
}

// analysisFor — последний разбор программы, в которую входит файл path.
func (s *Server) analysisFor(path string) (*gasem.Analysis, string) {
	root := s.rootFor(path)
	s.mu.Lock()
	a, ok := s.results[root]
	dirty := s.dirty[path]
	s.mu.Unlock()
	if !ok || dirty {
		a = s.analyze(root)
	}
	return a, root
}

// ---------------------------------------------------------------- главный файл программы

// rootFor — файл, с которого начинается программа, содержащая path: тот, что
// подключает path (через include, возможно цепочкой) и сам никем не подключён.
func (s *Server) rootFor(path string) string {
	includedBy := map[string][]string{}
	for _, f := range s.projectFiles(path) {
		text, ok := s.text(f)
		if !ok {
			continue
		}
		for _, inc := range includes(text, filepath.Dir(f)) {
			includedBy[inc] = append(includedBy[inc], f)
		}
	}
	seen := map[string]bool{path: true}
	queue := []string{path}
	var roots []string
	for len(queue) > 0 {
		f := queue[0]
		queue = queue[1:]
		parents := includedBy[f]
		if len(parents) == 0 && f != path {
			roots = append(roots, f)
		}
		for _, p := range parents {
			if !seen[p] {
				seen[p] = true
				queue = append(queue, p)
			}
		}
	}
	if len(roots) == 0 {
		return path
	}
	sort.Strings(roots)
	return roots[0]
}

// projectFiles — .gsm-файлы рядом с path и в папке проекта (не больше 2000).
func (s *Server) projectFiles(path string) []string {
	set := map[string]bool{}
	add := func(f string) {
		if strings.EqualFold(filepath.Ext(f), ".gsm") && len(set) < 2000 {
			set[normPath(f)] = true
		}
	}
	if entries, err := os.ReadDir(filepath.Dir(path)); err == nil {
		for _, e := range entries {
			if !e.IsDir() {
				add(filepath.Join(filepath.Dir(path), e.Name()))
			}
		}
	}
	s.mu.Lock()
	ws := s.workspace
	for d := range s.docs {
		add(d)
	}
	s.mu.Unlock()
	if ws != "" && strings.HasPrefix(path, ws) {
		_ = filepath.WalkDir(ws, func(p string, d os.DirEntry, err error) error {
			if err != nil || len(set) >= 2000 {
				return filepath.SkipDir
			}
			if d.IsDir() && p != ws && (strings.HasPrefix(d.Name(), ".") || d.Name() == "node_modules") {
				return filepath.SkipDir
			}
			if !d.IsDir() {
				add(p)
			}
			return nil
		})
	}
	out := make([]string, 0, len(set))
	for f := range set {
		out = append(out, f)
	}
	sort.Strings(out)
	return out
}

// includes — файлы, которые подключает текст (include "файл").
func includes(text, dir string) []string {
	var out []string
	for _, line := range strings.Split(text, "\n") {
		if !strings.Contains(strings.ToLower(line), "include") {
			continue
		}
		toks, err := gasem.SafeTokenize(line)
		if err != nil {
			continue
		}
		i := 0
		for i+1 < len(toks) && toks[i].Kind == gasem.ID && toks[i+1].IsOp(":") {
			i += 2
		}
		if i+1 < len(toks) && toks[i].IsID("include") && toks[i+1].Kind == gasem.STR {
			name := string(toks[i+1].Bytes)
			if !filepath.IsAbs(name) {
				name = filepath.Join(dir, name)
			}
			out = append(out, normPath(name))
		}
	}
	return out
}

// ---------------------------------------------------------------- диагностика

type diagnostic struct {
	Range              Range                `json:"range"`
	Severity           int                  `json:"severity"`
	Source             string               `json:"source"`
	Message            string               `json:"message"`
	RelatedInformation []relatedInformation `json:"relatedInformation,omitempty"`
}

type relatedInformation struct {
	Location Location `json:"location"`
	Message  string   `json:"message"`
}

func (s *Server) publish(root string, a *gasem.Analysis) {
	byFile := map[string][]diagnostic{}
	for _, f := range a.Files {
		byFile[normPath(f)] = []diagnostic{}
	}
	if _, ok := byFile[root]; !ok {
		byFile[root] = []diagnostic{}
	}
	add := func(e *gasem.Error, severity int) {
		if e.Loc == nil {
			byFile[root] = append(byFile[root], diagnostic{Severity: severity, Source: "gasem", Message: e.Message})
			return
		}
		file := normPath(e.Loc.File)
		d := diagnostic{Range: s.errorRange(file, e), Severity: severity, Source: "gasem", Message: e.Message}
		if top := e.Loc.Top(); top != e.Loc {
			// ошибка внутри макроса: показываем и в теле макроса, и в строке вызова
			callFile := normPath(top.File)
			call := Location{URI: pathToURI(callFile), Range: s.lineRange(callFile, top.Line-1)}
			d.Message += " (" + e.Loc.Via + ")"
			d.RelatedInformation = []relatedInformation{{call, "вызов макроса"}}
			byFile[callFile] = append(byFile[callFile], diagnostic{Range: call.Range, Severity: severity,
				Source: "gasem", Message: e.Message + " — в макросе (" + filepath.Base(file) +
					fmt.Sprintf(":%d)", e.Loc.Line),
				RelatedInformation: []relatedInformation{{Location{URI: pathToURI(file), Range: d.Range},
					"строка макроса"}}})
		}
		byFile[file] = append(byFile[file], d)
	}
	for _, e := range a.Errors {
		add(e, 1)
	}
	for _, w := range a.Warnings {
		add(w, 2)
	}
	s.mu.Lock()
	for f := range s.published {
		if _, ok := byFile[f]; !ok && s.rootForCached(f) == root {
			byFile[f] = []diagnostic{}
		}
	}
	for f, list := range byFile {
		if len(list) > 0 {
			s.published[f] = true
		} else {
			delete(s.published, f)
		}
	}
	s.mu.Unlock()
	files := make([]string, 0, len(byFile))
	for f := range byFile {
		files = append(files, f)
	}
	sort.Strings(files)
	for _, f := range files {
		_ = s.conn.notify("textDocument/publishDiagnostics", map[string]any{
			"uri": pathToURI(f), "diagnostics": byFile[f]})
	}
}

// rootForCached — главный файл, для которого публиковались диагностики path (без пересчёта).
func (s *Server) rootForCached(path string) string {
	for root, a := range s.results {
		for _, f := range a.Files {
			if normPath(f) == path {
				return root
			}
		}
	}
	return ""
}

// ---------------------------------------------------------------- позиции

func (s *Server) lineText(path string, line int) string {
	text, ok := s.text(path)
	if !ok {
		return ""
	}
	lines := strings.Split(text, "\n")
	if line < 0 || line >= len(lines) {
		return ""
	}
	return strings.TrimRight(lines[line], "\r")
}

// utf16Col — столбец в символах → столбец в единицах UTF-16.
func utf16Col(text string, col int) int {
	n, i := 0, 0
	for _, r := range text {
		if i >= col {
			break
		}
		n += len(utf16.Encode([]rune{r}))
		i++
	}
	return n + max(0, col-i)
}

// runeCol — столбец в единицах UTF-16 → столбец в символах.
func runeCol(text string, ch int) int {
	n, i := 0, 0
	for _, r := range text {
		if n >= ch {
			return i
		}
		n += len(utf16.Encode([]rune{r}))
		i++
	}
	return i
}

func (s *Server) lineRange(path string, line int) Range {
	text := s.lineText(path, line)
	start := len(text) - len(strings.TrimLeft(text, " \t"))
	return Range{Position{line, utf16Col(text, utf8.RuneCountInString(text[:start]))},
		Position{line, utf16Col(text, utf8.RuneCountInString(text))}}
}

func isIdentRune(r rune) bool {
	return r == '_' || r == '.' || r == '@' || ('0' <= r && r <= '9') || ('a' <= r && r <= 'z') ||
		('A' <= r && r <= 'Z') || r > 127 && (isLetterOrDigit(r))
}

// errorRange — место ошибки: слово, на которое указывает столбец, или вся строка.
func (s *Server) errorRange(path string, e *gasem.Error) Range {
	line := e.Loc.Line - 1
	if e.Col == gasem.NoCol || e.Loc.Via != "" {
		return s.lineRange(path, line)
	}
	text := e.Loc.Text
	rs := []rune(text)
	start := min(e.Col, len(rs))
	end := start
	for end < len(rs) && isIdentRune(rs[end]) {
		end++
	}
	if end == start && end < len(rs) {
		end++
	}
	return Range{Position{line, utf16Col(text, start)}, Position{line, utf16Col(text, end)}}
}

// wordAt — слово (имя) под курсором и его границы в символах.
func wordAt(text string, col int) (string, int, int) {
	rs := []rune(text)
	if col > len(rs) {
		col = len(rs)
	}
	start, end := col, col
	for start > 0 && isIdentRune(rs[start-1]) {
		start--
	}
	for end < len(rs) && isIdentRune(rs[end]) {
		end++
	}
	return string(rs[start:end]), start, end
}
