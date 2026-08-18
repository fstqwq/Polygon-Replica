local problem_id = os.getenv("STATEMENT_PREVIEW_ID") or "problem"
local render_root = os.getenv("STATEMENT_RENDER_ROOT") or "."


local function attr(classes, identifier)
  return pandoc.Attr(identifier or "", classes or {}, {})
end


local function resource_path(path)
  if path:match("^/") or path:match("^%a:[/\\]") or path == ".." or path:match("^%.%.[/\\]") or path:match("[/\\]%.%.[/\\]") or path:match("[/\\]%.%.$") then
    error("unsafe rendered statement resource: " .. path)
  end
  return render_root .. "/" .. path
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


local function parse_examples()
  local samples = {}
  local order = {}
  local current_pass = nil
  for line in read_text("examples.tex"):gmatch("[^\r\n]+") do
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


local function render_examples()
  local samples, order = parse_examples()
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


function RawBlock(element)
  if element.format ~= "latex" or not element.text:match("^\\begin%s*{problem}") then
    return nil
  end
  local name, input_file, output_file, time_limit, memory_limit, body = element.text:match(
    "^\\begin%s*{problem}{(.-)}{(.-)}{(.-)}{(.-)}{(.-)}\n(.*)\n\\end%s*{problem}$"
  )
  if not name then
    error("Pandoc did not preserve the rendered problem environment")
  end

  body = body:gsub("\\input%s*{examples%.tex}", "\n@@STATEMENT_EXAMPLES@@\n")
  body = body:gsub("\\InputFile", "\\subsubsection*{Input}")
  body = body:gsub("\\OutputFile", "\\subsubsection*{Output}")
  body = body:gsub("\\Interaction", "\\subsubsection*{Interaction}")
  body = body:gsub("\\par%s*", "")
  body = body:gsub(
    "\\ifdefined\\Note\n%s*\\ifx\\Note\\empty\n%s*\\subsection%*{Notes}\n%s*\\else\n%s*\\Note\n%s*\\fi\n\\else\n%s*\\subsection%*{Notes}\n\\fi\n",
    "\\subsubsection*{Notes}\n"
  )

  local before, after = body:match("^(.-)@@STATEMENT_EXAMPLES@@(.*)$")
  if not before then
    before = body
    after = ""
  end
  local output = pandoc.Blocks({
    pandoc.Header(2, name, attr({}, problem_id .. "-title")),
    pandoc.Div({pandoc.Plain({pandoc.Str(time_limit .. " · " .. memory_limit)})}, attr({"statement-meta"})),
  })
  append_all(output, prefix_headers(parse_latex(before)))
  append_all(output, render_examples())
  append_all(output, prefix_headers(parse_latex(after)))
  return pandoc.Div(output, attr({"statement-fragment"}))
end
