local problem_id = os.getenv("STATEMENT_PREVIEW_ID") or "problem"
local render_root = os.getenv("STATEMENT_RENDER_ROOT") or "."
local max_include_count = 64
local max_include_bytes = 2 * 1024 * 1024
local include_count = 0
local include_bytes = 0


local function attr(classes, identifier)
  return pandoc.Attr(identifier or "", classes or {}, {})
end


local function safe_relative_path(path)
  if path:match("^/") or path:match("^%a:[/\\]") or path:find("\\", 1, true) then
    error("unsafe rendered statement resource: " .. path)
  end
  local parts = {}
  for part in path:gmatch("[^/]+") do
    if part == ".." then
      error("unsafe rendered statement resource: " .. path)
    end
    if part ~= "." and part ~= "" then
      table.insert(parts, part)
    end
  end
  if #parts == 0 then
    error("empty rendered statement resource")
  end
  return table.concat(parts, "/")
end


local function resource_path(path)
  return render_root .. "/" .. safe_relative_path(path)
end


local function read_text(path)
  local file = io.open(resource_path(path), "rb")
  if not file then
    error("missing rendered statement resource: " .. path)
  end
  local content = file:read("*a")
  file:close()
  return content
end


local function include_path(value)
  local path = value:match("^%s*(.-)%s*$")
  if path == "" then
    error("empty rendered statement include")
  end
  if not path:match("%.[A-Za-z0-9]+$") then
    path = path .. ".tex"
  end
  return safe_relative_path(path)
end


local function include_argument(text, position, command)
  local cursor = position + #command + 1
  while text:sub(cursor, cursor):match("%s") do
    cursor = cursor + 1
  end
  if text:sub(cursor, cursor) == "{" then
    local close = text:find("}", cursor + 1, true)
    if not close then
      error("unterminated rendered statement include")
    end
    return text:sub(cursor + 1, close - 1), close + 1
  end
  local finish = cursor
  while finish <= #text and not text:sub(finish, finish):match("[%s%%]") do
    finish = finish + 1
  end
  if finish == cursor then
    error("rendered statement include has no path")
  end
  return text:sub(cursor, finish - 1), finish
end


local expand_includes


local function expanded_include(path, stack)
  include_count = include_count + 1
  if include_count > max_include_count then
    error("rendered statement has too many includes")
  end
  if stack[path] then
    error("cyclic rendered statement include: " .. path)
  end
  local content = read_text(path)
  include_bytes = include_bytes + #content
  if include_bytes > max_include_bytes then
    error("rendered statement includes exceed the preview limit")
  end
  stack[path] = true
  local expanded = expand_includes(content, stack)
  stack[path] = nil
  return expanded
end


expand_includes = function(text, stack)
  local output = {}
  local cursor = 1
  while cursor <= #text do
    local char = text:sub(cursor, cursor)
    if char == "%" then
      local newline = text:find("\n", cursor, true)
      if newline then
        table.insert(output, text:sub(cursor, newline))
        cursor = newline + 1
      else
        table.insert(output, text:sub(cursor))
        break
      end
    elseif char == "\\" then
      local next_char = text:sub(cursor + 1, cursor + 1)
      if next_char:match("[A-Za-z@]") then
        local finish = cursor + 2
        while text:sub(finish, finish):match("[A-Za-z@]") do
          finish = finish + 1
        end
        local command = text:sub(cursor + 1, finish - 1)
        if command == "input" then
          local value, after = include_argument(text, cursor, command)
          local path = include_path(value)
          table.insert(output, expanded_include(path, stack))
          cursor = after
        else
          table.insert(output, text:sub(cursor, finish - 1))
          cursor = finish
        end
      else
        table.insert(output, text:sub(cursor, math.min(cursor + 1, #text)))
        cursor = cursor + 2
      end
    else
      local next_special = text:find("[\\%%]", cursor + 1)
      if next_special then
        table.insert(output, text:sub(cursor, next_special - 1))
        cursor = next_special
      else
        table.insert(output, text:sub(cursor))
        break
      end
    end
  end
  return table.concat(output)
end


local function trim_final_newline(text)
  return text:gsub("\r\n", "\n"):gsub("\n$", "")
end


local function parse_latex(text)
  return pandoc.read(text, "latex+raw_tex+latex_macros").blocks
end


local function append_all(target, source)
  for _, item in ipairs(source) do
    target:insert(item)
  end
end


local function slug(text)
  local value = text:lower():gsub("[^%w]+", "-"):gsub("^-", ""):gsub("-$", "")
  if value == "" then
    return "section"
  end
  return value
end


local function prefix_headers(blocks)
  local container = pandoc.Div(blocks)
  local serial = 0
  return container:walk({
    Header = function(header)
      serial = serial + 1
      local label = pandoc.utils.stringify(header.content)
      header.identifier = problem_id .. "-" .. slug(label) .. "-" .. tostring(serial)
      return header
    end,
  }).content
end


local function code_file(path)
  return pandoc.CodeBlock(
    trim_final_newline(read_text(path)),
    attr({"sample-content"})
  )
end


local function get_sample(samples, order, number, presentation)
  local sample = samples[number]
  if not sample then
    sample = {number = number, presentation = presentation, passes = {}}
    samples[number] = sample
    table.insert(order, number)
  elseif sample.presentation ~= presentation then
    error("rendered sample mixes pair and interaction presentations: " .. number)
  end
  return sample
end


local function parse_examples(text)
  local samples = {}
  local order = {}
  local current_pass = nil
  local legacy_number = 0
  for raw_line in text:gmatch("[^\r\n]+") do
    local line = raw_line:match("^%s*(.-)%s*$")
    local sample_number, pass_number, input_path, output_path = line:match(
      "^\\StatementSamplePassFile{(.-)}{(.-)}{(.-)}{(.-)}$"
    )
    if sample_number then
      local sample = get_sample(samples, order, sample_number, "pair")
      table.insert(sample.passes, {
        number = pass_number,
        input_path = input_path,
        output_path = output_path,
      })
    end

    local single_number, single_input, single_output = line:match(
      "^\\StatementSampleFile{(.-)}{(.-)}{(.-)}$"
    )
    if single_number then
      local sample = get_sample(samples, order, single_number, "pair")
      table.insert(sample.passes, {
        number = "1",
        input_path = single_input,
        output_path = single_output,
      })
    end

    local legacy_input, legacy_output = line:match(
      "^\\exmpfile{(.-)}{(.-)}%%$"
    )
    if not legacy_input then
      legacy_input, legacy_output = line:match(
        "^\\exmpfile{(.-)}{(.-)}$"
      )
    end
    if legacy_input then
      legacy_number = legacy_number + 1
      local number = tostring(legacy_number)
      local sample = get_sample(samples, order, number, "pair")
      table.insert(sample.passes, {
        number = "1",
        input_path = legacy_input,
        output_path = legacy_output,
      })
    end

    local pass_number_optional, interaction_number = line:match(
      "^\\begin{StatementSampleInteraction}%[(.-)%]{(.-)}$"
    )
    if not interaction_number then
      interaction_number = line:match("^\\begin{StatementSampleInteraction}{(.-)}$")
      pass_number_optional = "1"
    end
    if interaction_number then
      local sample = get_sample(samples, order, interaction_number, "interaction")
      current_pass = {number = pass_number_optional, events = {}}
      table.insert(sample.passes, current_pass)
    end

    local source, event_path = line:match(
      "^\\StatementSampleEventFile{(.-)}{(.-)}$"
    )
    if source then
      if not current_pass then
        error("rendered interaction event is outside a pass")
      end
      table.insert(current_pass.events, {source = source, path = event_path})
    end

    if line:match("^\\end{StatementSampleInteraction}$") then
      current_pass = nil
    end
  end
  if #order == 0 then
    error("rendered statement sample block contains no supported samples")
  end
  return samples, order
end


local function role_panel(label, path)
  return pandoc.Div({
    pandoc.Div({pandoc.Plain({pandoc.Str(label)})}, attr({"sample-role"})),
    code_file(path),
  }, attr({"sample-panel"}))
end


local function render_pair_pass(pass, sample_number, multiple_passes)
  local prefix = "Sample " .. sample_number
  if multiple_passes then
    prefix = prefix .. " Pass " .. pass.number
  end
  return pandoc.Div({
    role_panel(prefix .. " Input", pass.input_path),
    role_panel(prefix .. " Output", pass.output_path),
  }, attr({"sample-pair"}))
end


local function render_interaction_pass(pass, label)
  local blocks = pandoc.Blocks({
    pandoc.Div({
      pandoc.Div({pandoc.Plain({pandoc.Str("Read")})}, attr({"interaction-heading-side"})),
      pandoc.Div({pandoc.Plain({pandoc.Str(label)})}, attr({"interaction-heading-label"})),
      pandoc.Div({pandoc.Plain({pandoc.Str("Write")})}, attr({"interaction-heading-side"})),
    }, attr({"interaction-heading"})),
  })
  for _, event in ipairs(pass.events) do
    local source_class = ""
    if event.source == "interactor" then
      source_class = "interaction-event-interactor"
    elseif event.source == "solution" then
      source_class = "interaction-event-solution"
    else
      error("rendered interaction has unknown source: " .. event.source)
    end
    blocks:insert(pandoc.Div(
      {code_file(event.path)},
      attr({"interaction-event", source_class})
    ))
  end
  return pandoc.Div(blocks, attr({"interaction-events"}))
end


local function render_examples(text)
  local samples, order = parse_examples(text)
  local output = pandoc.Blocks({
    pandoc.Header(3, "Examples", attr({}, problem_id .. "-examples")),
  })
  for _, number in ipairs(order) do
    local sample = samples[number]
    local sample_blocks = pandoc.Blocks({})
    local multiple_passes = #sample.passes > 1
    for _, pass in ipairs(sample.passes) do
      local rendered
      if sample.presentation == "pair" then
        rendered = render_pair_pass(pass, sample.number, multiple_passes)
      else
        local label = "Sample Interaction " .. sample.number
        if multiple_passes then
          label = "Sample " .. sample.number .. ", Pass " .. pass.number
        end
        rendered = render_interaction_pass(pass, label)
      end
      sample_blocks:insert(pandoc.Div({rendered}, attr({"statement-pass"})))
    end
    output:insert(pandoc.Div(sample_blocks, attr({"statement-sample"})))
  end
  return output
end


local function sample_environment(text, cursor, name)
  local start_at, start_end = text:find("\\begin%s*{" .. name .. "}", cursor)
  if not start_at then
    return nil
  end
  local end_at, end_end = text:find("\\end%s*{" .. name .. "}", start_end + 1)
  if not end_at then
    error("unterminated rendered statement sample environment: " .. name)
  end
  return {
    start_at = start_at,
    end_at = end_end,
    text = text:sub(start_at, end_end),
  }
end


local function next_sample_environment(text, cursor)
  local structured = sample_environment(text, cursor, "StatementSamples")
  local legacy = sample_environment(text, cursor, "example")
  local selected = structured
  if not selected or (legacy and legacy.start_at < selected.start_at) then
    selected = legacy
  end
  if not selected then
    return nil
  end
  local prefix = text:sub(cursor, selected.start_at - 1)
  local heading = prefix:match("()\\Examples?%s*$")
  if heading then
    selected.start_at = cursor + heading - 1
    selected.text = text:sub(selected.start_at, selected.end_at)
  end
  return selected
end


local function render_body(body)
  local output = pandoc.Blocks({})
  local cursor = 1
  while cursor <= #body do
    local sample = next_sample_environment(body, cursor)
    if not sample then
      append_all(output, prefix_headers(parse_latex(body:sub(cursor))))
      break
    end
    if sample.start_at > cursor then
      append_all(
        output,
        prefix_headers(parse_latex(body:sub(cursor, sample.start_at - 1)))
      )
    end
    append_all(output, render_examples(sample.text))
    cursor = sample.end_at + 1
  end
  return output
end


local function normalize_legacy_note_guard(body)
  local guard =
    "\\ifdefined%s*\\Note%s*" ..
    "\\ifx%s*\\Note%s*\\empty%s*" ..
    "\\subsection%s*%*%s*%{Notes%}%s*" ..
    "\\else%s*\\Note%s*\\fi%s*" ..
    "\\else%s*\\subsection%s*%*%s*%{Notes%}%s*\\fi"
  return (body:gsub(guard, "\\Note\n"))
end


local function remove_empty_trailing_note_marker(body)
  body = body:gsub("\n%s*\\Notes%s*$", "")
  body = body:gsub("\n%s*\\Note%s*$", "")
  body = body:gsub("^%s*\\Notes%s*$", "")
  body = body:gsub("^%s*\\Note%s*$", "")
  return body
end


local section_commands = {
  InputFile = "Input",
  OutputFile = "Output",
  Interaction = "Interaction",
  Note = "Note",
  Notes = "Notes",
}


local function translate_standalone_section_commands(body)
  return (body:gsub("[^\n]+", function(line)
    local command = line:match("^%s*\\([A-Za-z]+)%s*$")
    local heading = section_commands[command]
    if heading then
      return "\\subsubsection*{" .. heading .. "}"
    end
    return line
  end))
end


function RawBlock(element)
  if element.format ~= "latex" or not element.text:match("^\\begin%s*{problem}") then
    return nil
  end
  include_count = 0
  include_bytes = 0
  local rendered_problem = read_text("problem.tex"):gsub("\r\n", "\n")
  rendered_problem = expand_includes(
    rendered_problem,
    { ["problem.tex"] = true }
  )
  local name, input_file, output_file, time_limit, memory_limit, body = rendered_problem:match(
    "^%s*\\begin%s*{problem}{(.-)}{(.-)}{(.-)}{(.-)}{(.-)}\n(.-)\n\\end%s*{problem}%s*$"
  )
  if not name then
    error("rendered problem.tex does not contain a supported problem environment")
  end

  body = normalize_legacy_note_guard(body)
  body = remove_empty_trailing_note_marker(body)
  body = translate_standalone_section_commands(body)
  body = body:gsub("\\par%s*", "")

  local output = pandoc.Blocks({
    pandoc.Header(2, name, attr({}, problem_id .. "-title")),
    pandoc.Div({pandoc.Plain({pandoc.Str(time_limit .. " · " .. memory_limit)})}, attr({"statement-meta"})),
  })
  append_all(output, render_body(body))
  return pandoc.Div(output, attr({"statement-fragment"}))
end
