-- Own TeX presentation scope and layout; emit only bounded HTML attributes.
local M = {}
local boxes = {}
local box_serial = 0
local box_prefix = 'StatementParbox'..pandoc.utils.sha1(tostring({}))..'-'
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

local function argument(text, cursor, opening, closing)
  cursor = text:find('%S', cursor) or (#text + 1)
  if text:sub(cursor,cursor) ~= opening then return nil,cursor end
  local start, depth = cursor + 1, 1
  cursor = start
  while cursor <= #text do
    local char = text:sub(cursor,cursor)
    if char == '\\' then cursor = cursor + 2
    elseif char == '%' then cursor = text:find('\n',cursor,true) or (#text+1)
    else
      if char == opening then depth=depth+1 end
      if char == closing then depth=depth-1 end
      if depth == 0 then return text:sub(start,cursor-1),cursor+1 end
      cursor=cursor+1
    end
  end
  error('unterminated presentation argument')
end

local function presentation_environment(text, cursor)
  local opening = text:find('%S',cursor)
  if not opening or text:sub(opening,opening) ~= '{' then return nil,cursor end
  -- Unknown names must not scan an arbitrarily long, possibly unclosed argument.
  for _,name in ipairs({'verbatim','Verbatim','lstlisting','minted','minipage','tabular'}) do
    local finish = opening+#name+1
    if text:sub(opening+1,finish) == name..'}' then return name,finish+1 end
  end
  return nil,cursor
end

-- Keep box and table boundaries before Pandoc's LaTeX reader flattens them.
function M.prepare(text)
  local output, cursor = {}, 1
  while cursor <= #text do
    local char = text:sub(cursor,cursor)
    local command = char == '\\' and text:match('^\\([A-Za-z]+)',cursor)
    if char == '%' then
      local finish = text:find('\n',cursor,true) or (#text+1)
      table.insert(output,text:sub(cursor,finish-1)); cursor=finish
    elseif command == 'verb' then
      local start = cursor + 5
      if text:sub(start,start) == '*' then start=start+1 end
      local finish = text:find(text:sub(start,start),start+1,true)
      if not finish then error('unterminated verbatim text') end
      table.insert(output,text:sub(cursor,finish)); cursor=finish+1
    elseif command == 'begin' or command == 'end' then
      local name,after = presentation_environment(text,cursor+#command+1)
      if name and command == 'begin' and ({verbatim=true,Verbatim=true,lstlisting=true,minted=true})[name] then
        local _,finish=text:find('\\end{'..name..'}',after,true)
        if not finish then error('unterminated verbatim environment') end
        table.insert(output,text:sub(cursor,finish)); cursor=finish+1
      elseif name == 'minipage' or name == 'tabular' then
        local replacement = name == 'minipage' and 'StatementMinipage' or 'StatementTabular'
        table.insert(output,'\\'..command..'{'..replacement..'}')
        cursor=after
        if name == 'minipage' and command == 'begin' then
          local option = text:find('%S',cursor) or (#text+1)
          if text:sub(option,option) ~= '[' then table.insert(output,'[c]') end
        end
      else
        table.insert(output,'\\'..command); cursor=cursor+#command+1
      end
    elseif command == 'parbox' then
      local finish = cursor+#command+1
      for _=1,3 do
        local option,next_cursor=argument(text,finish,'[',']')
        if not option then break end
        finish=next_cursor
      end
      local width,next_cursor=argument(text,finish,'{','}')
      local body,end_cursor=argument(text,next_cursor,'{','}')
      if not width or not body then error('incomplete parbox') end
      -- Code nodes survive even readers that flatten raw TeX in table cells.
      box_serial=box_serial+1
      local token=box_prefix..tostring(box_serial)
      boxes[token]=text:sub(cursor,end_cursor-1)
      table.insert(output,'\\texttt{'..token..'}')
      cursor=end_cursor
    elseif command then table.insert(output,'\\'..command); cursor=cursor+#command+1
    elseif char == '\\' then table.insert(output,text:sub(cursor,cursor+1)); cursor=cursor+2
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
local parbox

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
    if item.t == 'Code' and boxes[item.text] then
      local box=parbox(boxes[item.text],state)
      run:insert(pandoc.Span(pandoc.utils.blocks_to_inlines(box.content),box.attr))
      handled=true
    end
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
      if item.t == 'Image' then
        -- Pandoc expresses TeX textwidth/linewidth dimensions as percentages.
        -- Tables must not make those percentages relative to a cell.
        for _,key in ipairs({'width','height'}) do
          local value = item.attributes[key]
          if value and value:match('^[%d.]+%%$') then
            item.attributes.style = (item.attributes.style or '')..';'..key..':'..value:gsub('%%$','cqw')
            item.attributes[key] = nil
          end
        end
      elseif item.t == 'Note' then item.content = blocks(item.content, copy(state))
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

parbox = function(text, state)
  local command = text:match('^\\parbox%s*')
  if not command then return nil end
  local cursor, options = #command+1, {}
  for index=1,3 do
    local option, next_cursor = argument(text,cursor,'[',']')
    if not option then break end
    options[index],cursor = option,next_cursor
  end
  local width, next_cursor = argument(text,cursor,'{','}')
  local body, finish = argument(text,next_cursor,'{','}')
  if not width or not body or text:sub(finish):match('%S') then return nil end
  local size = dimension(width,'pt')
  if not size then error('unsupported parbox width: '..width) end
  size = size:gsub('%%$','cqw')
  local alignment = ({t='top',c='middle',b='bottom'})[options[1] or 'c']
  if not alignment then error('unsupported parbox alignment') end
  local scoped = copy(state)
  local content = blocks(pandoc.read(M.prepare(body),'latex+raw_tex+latex_macros').blocks,scoped)
  local styles = 'width:'..size..';vertical-align:'..alignment
  if scoped.align then styles=styles..';text-align:'..scoped.align end
  if options[2] then
    local height = dimension(options[2],'pt')
    if not height then error('unsupported parbox height: '..options[2]) end
    styles=styles..';height:'..height:gsub('%%$','cqw')
  end
  local inner = options[3] or options[1] or 'c'
  if not ({t=true,c=true,b=true,s=true})[inner] then error('unsupported parbox inner alignment') end
  return pandoc.Div(content,pandoc.Attr('',{'statement-parbox','statement-parbox-'..inner},{style=styles}))
end

local function tabular(text)
  local opening = text:match('^\\begin{StatementTabular}')
  if not opening then return nil end
  local position,cursor = argument(text,#opening+1,'[',']')
  local spec,body_start = argument(text,cursor,'{','}')
  if not spec then return nil end
  local body = text:sub(body_start):match('^(.*)\\end{StatementTabular}%s*$')
  if not body then return nil end
  local parsed = pandoc.read('\\begin{tabular}'..(position and '['..position..']' or '')..
    '{'..spec..'}'..body..'\\end{tabular}','latex+raw_tex+latex_macros').blocks
  if #parsed ~= 1 or parsed[1].t ~= 'Table' then error('unsupported tabular structure') end
  local result = parsed[1]
  result.classes:insert('statement-tabular')
  -- @{...} replaces the padding on both sides of this column boundary.
  local boundaries, column, index = {}, 0, 1
  while index <= #spec do
    local token=spec:sub(index,index)
    if token == '@' then
      local spacing, next_index = argument(spec,index+1,'{','}')
      if not spacing then break end
      spacing=spacing:match('^%s*(.-)%s*$')
      local value = ({['']='0pt',['\\quad']='1em',['\\qquad']='2em'})[spacing]
      local explicit = spacing:match('^\\hspace%*?%s*{(.-)}$')
      if explicit then value=dimension(explicit,'pt') end
      if value then boundaries[column]=value end
      index=next_index
    elseif token:match('[lcrpmb]') then
      column=column+1; index=index+1
      if token:match('[pmb]') then local _,finish=argument(spec,index,'{','}'); index=finish end
    elseif token == '{' then local _,finish=argument(spec,index,'{','}'); index=finish
    else index=index+1 end
  end
  if column ~= #result.colspecs then return result end
  local function rows(entries)
    for _,row in ipairs(entries) do
      local start=0
      for _,cell in ipairs(row.cells) do
        local styles={}
        if boundaries[start] then table.insert(styles,'padding-left:'..boundaries[start]) end
        start=start+cell.col_span
        if boundaries[start] then table.insert(styles,'padding-right:0pt') end
        if #styles>0 then cell.attributes.style=table.concat(styles,';') end
      end
    end
  end
  rows(result.head.rows); rows(result.foot.rows)
  for _,body_part in ipairs(result.bodies) do rows(body_part.head); rows(body_part.body) end
  return result
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
      local table_block = tabular(item.text)
      local vertical = item.text:match('^\\vspace%*?%s*(%b{})%s*$')
      local font, body, ending = item.text:match('^\\begin%s*{([A-Za-z]+)}(.*)\\end%s*{([A-Za-z]+)}%s*$')
      if page then item = page; prepared = true
      elseif table_block then item=table_block
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
    if not handled and (item.t == 'Para' or item.t == 'Plain') and #item.content == 1 and
        item.content[1].t == 'Code' and boxes[item.content[1].text] then
      local box = parbox(boxes[item.content[1].text],state)
      if box then item=box; prepared=true end
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
        if state.align then item.attributes.style='text-align:'..state.align end
        local function rows(entries)
          for _,row in ipairs(entries) do
            for _,cell in ipairs(row.cells) do
              cell.contents=blocks(cell.contents,copy(state))
              local box=cell.contents[1]
              if #cell.contents == 1 and box.t == 'Div' and box.classes:includes('statement-parbox') then
                local alignment=box.attributes.style:match('vertical%-align:([^;]+)')
                cell.attributes.style=(cell.attributes.style or '')..';vertical-align:'..alignment
              end
            end
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
