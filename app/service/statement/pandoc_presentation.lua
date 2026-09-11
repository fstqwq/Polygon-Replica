-- Own TeX presentation scope and layout; emit only bounded HTML attributes.
local M = {}
local sizes = {tiny=7.64, scriptsize=10.18, footnotesize=11.45, small=12.73,
  normalsize=14, large=15.27, Large=18.33, LARGE=21.99, huge=26.40, Huge=31.67}

local function copy(state)
  local result = {}
  for key, value in pairs(state) do result[key] = value end
  return result
end

local function dimension(text, default_unit)
  text = text:match('^%s*(.-)%s*$')
  local number, unit = text:match('^([%d.]+)%s*(%a*)$')
  if text == '\\textwidth' or text == '\\linewidth' then return '100%' end
  local fraction = text:match('^([%d.]+)\\textwidth$') or text:match('^([%d.]+)\\linewidth$')
  if fraction then
    local value = tonumber(fraction)
    if value and value > 0 and value <= 1 then return tostring(value * 100) .. '%' end
    return nil
  end
  local value = tonumber(number)
  unit = unit == '' and default_unit or unit
  if value and value >= 0 and value <= 1000 and
      ({pt=true,pc=true,cm=true,mm=true,['in']=true,em=true,ex=true,px=true})[unit] then
    return tostring(value) .. unit
  end
  return nil
end

-- Keep minipage boundaries before Pandoc's LaTeX reader flattens them.
function M.prepare(text)
  local output, cursor = {}, 1
  while cursor <= #text do
    local tail = text:sub(cursor)
    local comment = tail:match('^%%[^\n]*')
    local command = tail:match('^\\([A-Za-z]+)')
    if comment then table.insert(output,comment); cursor=cursor+#comment
    elseif command == 'verb' then
      local start = cursor + 5
      if text:sub(start,start) == '*' then start=start+1 end
      local finish = text:find(text:sub(start,start),start+1,true)
      if not finish then error('unterminated verbatim text') end
      table.insert(output,text:sub(cursor,finish)); cursor=finish+1
    elseif command == 'begin' or command == 'end' then
      local opening, name = tail:match('^(\\%a+%s*{([^}]+)})')
      if opening and command == 'begin' and ({verbatim=true,Verbatim=true,lstlisting=true,minted=true})[name] then
        local _,finish=text:find('\\end{'..name..'}',cursor+#opening,true)
        if not finish then error('unterminated verbatim environment') end
        table.insert(output,text:sub(cursor,finish)); cursor=finish+1
      elseif opening and name == 'minipage' then
        table.insert(output,'\\'..command..'{StatementMinipage}')
        cursor=cursor+#opening
        if command == 'begin' and not text:sub(cursor):match('^%s*%[') then table.insert(output,'[c]') end
      else
        table.insert(output,'\\'..command); cursor=cursor+#command+1
      end
    elseif command then table.insert(output,'\\'..command); cursor=cursor+#command+1
    elseif tail:sub(1,1) == '\\' then table.insert(output,tail:sub(1,2)); cursor=cursor+2
    else
      local next_special=text:find('[\\%%]',cursor+1) or (#text+1)
      table.insert(output,text:sub(cursor,next_special-1)); cursor=next_special
    end
  end
  return table.concat(output)
end

local function style(state)
  local values = {}
  if state.size then table.insert(values, 'font-size:' .. state.size) end
  if state.leading then table.insert(values, 'line-height:' .. state.leading) end
  return table.concat(values, ';')
end

local function declaration(text, state)
  local command = text:match('^\\([A-Za-z]+)%s*$')
  if sizes[command] then
    state.size = tostring(sizes[command]) .. 'px'
    state.leading = nil
    return true
  end
  local size, leading = text:match('^\\fontsize%s*(%b{})%s*(%b{})%s*$')
  if size then
    size, leading = dimension(size:sub(2,-2),'pt'), dimension(leading:sub(2,-2),'pt')
    if size and leading then
      state.pending_size, state.pending_leading = size, leading
      return true
    end
  end
  if command == 'selectfont' then
    if state.pending_size then
      state.size, state.leading = state.pending_size, state.pending_leading
      state.pending_size, state.pending_leading = nil, nil
    end
    return true
  end
  local alignments = {centering='center', raggedright='left', raggedleft='right'}
  if alignments[command] then state.align = alignments[command]; return true end
  local caption = text:match('^\\captionsetup%s*{%s*font%s*=%s*([A-Za-z]+)%s*}%s*$')
  if sizes[caption] then state.caption_size=tostring(sizes[caption])..'px'; return true end
  return false
end

local blocks
local inlines

inlines = function(content, state, groups)
  local output, run = pandoc.Inlines({}), pandoc.Inlines({})
  local run_style = style(state)
  local stack = groups or {}
  local function flush()
    if #run > 0 then
      if run_style ~= '' then
        output:insert(pandoc.Span(run, pandoc.Attr('', {'statement-font'}, {style=run_style})))
      else output:extend(run) end
    end
    run = pandoc.Inlines({})
  end
  for _, item in ipairs(content) do
    local handled = false
    if item.t == 'RawInline' and item.format == 'latex' then
      local command = item.text:match('^\\([A-Za-z]+)%s*$')
      if command == 'begingroup' then
        flush(); table.insert(stack, copy(state)); handled = true
      elseif command == 'endgroup' and #stack > 0 then
        flush(); local saved = table.remove(stack)
        for key in pairs(state) do state[key] = nil end
        for key,value in pairs(saved) do state[key] = value end
        handled = true
      else
        local next_state = copy(state)
        if declaration(item.text, next_state) then
          flush()
          for key in pairs(state) do state[key] = nil end
          for key,value in pairs(next_state) do state[key] = value end
          handled = true
        end
      end
      if handled then run_style = style(state) end
      local spacing = item.text:match('^\\hspace%*?%s*(%b{})%s*$')
      if not handled and spacing then
        local width = dimension(spacing:sub(2,-2),'pt')
        if width then
          run:insert(pandoc.Span({},pandoc.Attr('',{'statement-hspace'},{style='width:'..width})))
          handled = true
        end
      end
    end
    if not handled then
      if item.t == 'Note' then item.content = blocks(item.content, copy(state))
      elseif item.t == 'Span' or item.t == 'Strong' or item.t == 'Emph' or
          item.t == 'Strikeout' or item.t == 'SmallCaps' or item.t == 'Superscript' or
          item.t == 'Subscript' or item.t == 'Link' or item.t == 'Quoted' or item.t == 'Cite' then
        item.content = inlines(item.content, copy(state))
      end
      run:insert(item)
    end
  end
  flush()
  return output
end

local function minipage(text, state)
  local alignment, width, body = text:match('^\\begin{StatementMinipage}%s*%[([^]]*)%]%s*(%b{})(.*)\\end{StatementMinipage}%s*$')
  if not body then return nil end
  local size = dimension(width:sub(2,-2),'pt')
  if not size then error('unsupported minipage width: ' .. width) end
  local align = ({t='top',c='middle',b='bottom'})[alignment]
  if not align then error('unsupported minipage alignment: ' .. alignment) end
  local content = blocks(pandoc.read(M.prepare(body),'latex+raw_tex+latex_macros').blocks,copy(state))
  return pandoc.Div(content,pandoc.Attr('',{'statement-minipage'},{style='width:'..size..';vertical-align:'..align}))
end

local function is_minipage(item)
  return item.t == 'Div' and item.classes:includes('statement-minipage')
end

local function spacing_block(item)
  local text = ''
  if item.t == 'RawBlock' and item.format == 'latex' then text=item.text
  elseif item.t == 'Para' or item.t == 'Plain' then
    for _, inline in ipairs(item.content) do
      if inline.t == 'RawInline' and inline.format == 'latex' then text = text .. inline.text
      elseif inline.t == 'Span' and inline.classes:includes('statement-hspace') then
        return inline.attributes.style:match('^width:(.*)$')
      elseif inline.t ~= 'Space' and inline.t ~= 'SoftBreak' then return nil end
    end
  else return nil
  end
  if text:match('^%s*\\hfill%s*$') then return 'fill' end
  local value = text:match('^%s*\\hspace%*?%s*(%b{})%s*$')
  if value then return dimension(value:sub(2,-2),'pt') end
  return nil
end

blocks = function(content, state)
  local output, stack = pandoc.Blocks({}), {}
  local index = 1
  while index <= #content do
    local item = content[index]
    local handled = false
    local prepared = false
    if item.t == 'RawBlock' and item.format == 'latex' then
      local page = minipage(item.text,state)
      local vertical = item.text:match('^\\vspace%*?%s*(%b{})%s*$')
      local font, body, ending = item.text:match('^\\begin%s*{([A-Za-z]+)}(.*)\\end%s*{([A-Za-z]+)}%s*$')
      if page then item = page; prepared = true
      elseif sizes[font] and ending == font then
        local scoped=copy(state)
        declaration('\\'..font,scoped)
        item=pandoc.Div(blocks(pandoc.read(M.prepare(body),'latex+raw_tex+latex_macros').blocks,scoped))
        item.attributes.style=style(scoped); prepared=true
      elseif item.text:match('^\\begingroup%s*$') then
        table.insert(stack,copy(state)); handled=true
      elseif item.text:match('^\\endgroup%s*$') and #stack > 0 then
        state=table.remove(stack); handled=true
      elseif vertical and dimension(vertical:sub(2,-2),'pt') then
        item=pandoc.Div({},pandoc.Attr('',{}, {style='margin-top:'..dimension(vertical:sub(2,-2),'pt')}))
      elseif declaration(item.text,state) then handled = true end
    end
    if not handled and (item.t == 'Para' or item.t == 'Plain') then
      -- Groups may straddle separately parsed sample environments.
      local raw = #item.content == 1 and item.content[1].t == 'RawInline' and item.content[1].text or ''
      if raw:match('^\\begingroup%s*$') then table.insert(stack,copy(state)); handled=true
      elseif raw:match('^\\endgroup%s*$') and #stack > 0 then state=table.remove(stack); handled=true
      end
    end
    if not handled then
      if item.t == 'Para' or item.t == 'Plain' or item.t == 'Header' then
        local vertical = #item.content == 1 and item.content[1].t == 'RawInline' and
          item.content[1].text:match('^\\vspace%*?%s*(%b{})%s*$')
        if vertical then
          local value = dimension(vertical:sub(2,-2),'pt')
          if value then item=pandoc.Div({},pandoc.Attr('',{}, {style='margin-top:'..value})) end
        else
          item.content = inlines(item.content,state,stack)
          local paragraph_style = state.align and 'text-align:'..state.align or ''
          if #item.content == 1 and item.content[1].t == 'Span' and item.content[1].classes:includes('statement-font') then
            paragraph_style=paragraph_style..';'..item.content[1].attributes.style
          end
          if paragraph_style ~= '' then item=pandoc.Div({item},pandoc.Attr('',{'statement-paragraph'}, {style=paragraph_style})) end
        end
      elseif item.t == 'Div' or item.t == 'BlockQuote' then
        if not prepared then item.content = blocks(item.content,copy(state)) end
        if item.t == 'Div' and style(state) ~= '' and not prepared then
          item.attributes.style = style(state)
        end
      elseif item.t == 'BulletList' or item.t == 'OrderedList' then
        for i,entry in ipairs(item.content) do item.content[i]=blocks(entry,copy(state)) end
      elseif item.t == 'Figure' then
        local scoped=copy(state)
        item.content=blocks(item.content,scoped)
        local caption_state=copy(scoped)
        caption_state.size=scoped.caption_size or scoped.size
        item.caption.long=blocks(item.caption.long,caption_state)
        if scoped.align then item.attributes.style='text-align:'..scoped.align end
      elseif item.t == 'Table' then
        local function rows(entries)
          for _,row in ipairs(entries) do
            for _,cell in ipairs(row.cells) do cell.contents=blocks(cell.contents,copy(state)) end
          end
        end
        rows(item.head.rows); rows(item.foot.rows)
        for _,body in ipairs(item.bodies) do rows(body.head); rows(body.body) end
        item.caption.long=blocks(item.caption.long,copy(state))
      elseif item.t == 'DefinitionList' then
        for _,entry in ipairs(item.content) do
          entry[1]=inlines(entry[1],copy(state))
          for i,definition in ipairs(entry[2]) do entry[2][i]=blocks(definition,copy(state)) end
        end
      end
      output:insert(item)
    end
    index = index + 1
  end
  -- A run of minipages and explicit horizontal separators forms one row.
  local grouped = pandoc.Blocks({})
  index = 1
  while index <= #output do
    if is_minipage(output[index]) then
      local row = pandoc.Blocks({output[index]})
      index = index + 1
      while index <= #output do
        if is_minipage(output[index]) then row:insert(output[index]); index=index+1
        elseif spacing_block(output[index]) and index < #output and is_minipage(output[index+1]) then
          local gap=spacing_block(output[index])
          row:insert(pandoc.Div({},pandoc.Attr('',{gap=='fill' and 'statement-fill' or 'statement-gap'},gap=='fill' and {} or {style='width:'..gap})))
          index=index+1
        else break end
      end
      grouped:insert(pandoc.Div(row,pandoc.Attr('',{'statement-columns'},{})))
    else grouped:insert(output[index]); index=index+1 end
  end
  return grouped
end

function M.render(content)
  return blocks(content,{})
end

return M
