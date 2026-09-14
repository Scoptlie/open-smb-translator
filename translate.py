
import io
import re

class Line:
	def __init__(self: Line, pre_comment: str, text: str, post_comment: str):
		self.pre_comment = pre_comment
		self.text = text
		self.post_comment = post_comment

class Label:
	def __init__(self: Label, line: int, kind: str, name: str):
		self.line = line
		self.kind = kind
		self.name = name

global text_lines
global lines
global labels
global sub_label_names
global data_label_names

text_lines: list[str] = []
lines: list[Line] = []
labels: list[Label] = []
c: str = ''
sub_label_names: list[str] = []
data_label_names: list[str] = []

def parse_lines(text_lines: list[str]):
	first = text_lines.index('    .org $8000\n') + 1
	last = text_lines.index('    .word NonMaskableInterrupt ;NMI vector\n') - 1
	
	pre_comment = '\n'
	for i in range(first, last + 1):
		text_line = text_lines[i]
		
		try:
			semi = text_line.index(';')
			text = text_line[:semi].strip()
			comment = text_line[semi + 1:].strip()
		except ValueError:
			text = text_line.strip()
			comment = ''
		
		if text == '':
			if comment != '':
				pre_comment += comment + '\n'
		else:
			lines.append(Line(pre_comment[:-1], text, comment))
			pre_comment = '\n'

def find_labels():
	for i, line in enumerate(lines):
		if ':' in line.text:
			if line.text in ['Start:', 'NonMaskableInterrupt:']:
				kind = 'sub'
			elif lines[i + 1].text.startswith('.'):
				kind = 'data'
			else:
				kind = 'local'
			
			name = line.text.split(':')[0]
			
			labels.append(Label(i, kind, name))

def find_subs():
	for i, label in enumerate(labels):
		if i > 0 and label.kind == 'local' and labels[i - 1].kind == 'data':
			label.kind = 'sub'
	
	for i, label in enumerate(labels):
		if label.kind != 'local':
			continue
		
		for j, line in enumerate(lines):
			if re.search(r'\b' + re.escape(label.name) + r'\b', line.text):
				if j == label.line:
					continue
				
				if len(line.text) == 4 + len(label.name) and\
					line.text[0:3] in ['jmp', 'bpl', 'bmi', 'bvc', 'bvs', 'bcc', 'bcs', 'bne', 'beq']:
					
					continue
				
				label.kind = 'sub'

def find_more_subs() -> bool:
	sub_starts = [label.line for label in labels if label.kind == 'sub']
	local_labels = [label for label in labels if label.kind == 'local']
	
	r = False
	
	for label in local_labels:
		sub_containing_label = len(sub_starts) - 1
		for i in range(0, len(sub_starts) - 1):
			if label.line >= sub_starts[i] and label.line < sub_starts[i + 1]:
				sub_containing_label = i
				break
		
		current_sub = 0
		for i, line in enumerate(lines):
			if current_sub + 1 < len(sub_starts) and i == sub_starts[current_sub + 1]:
				current_sub += 1
			
			pattern = re.compile(r'\b' + re.escape(label.name) + r'\b')
			
			if re.search(pattern, line.text):
				if i == label.line:
					continue
				
				if sub_containing_label != current_sub:
					label.kind = 'sub'
					r = True
					break
	
	return r

def operand_as_c(text: str, addr_mode: str):
	if addr_mode == 'code':
		return text
	
	if addr_mode == 'imm':
		if text.startswith('$'):
			return '0x' + text[1:]
		elif text.startswith('%'):
			return '0b' + text[1:]
		elif text.startswith('<'):
			return '(' + text[1:] + ' & 0xff)'
		elif text.startswith('>'):
			return '(' + text[1:] + ' >> 8)'
		else:
			return '(' + text.replace('$', '0x').replace('%', '0xb') + ')'
	
	if text.startswith('$') or text.startswith('%'):
		if text.startswith('$'):
			addr = int(text[1:], 16)
		else:
			addr = int(text[1:], 2)
		
		if addr >= 0x8000:
			base = 'rom'
		else:
			base = 'ram'
		
		index = f'0x{(addr & 0x7fff):04x}'
	else:
		label = re.search(r'[A-Za-z0-9_]+', text)
		if label is not None and label.string in data_label_names:
			base = 'rom'
		else:
			base = 'ram'
		
		index = text.replace('$', '0x').replace('%', '0xb')
	
	if addr_mode == 'abs':
		return base + '[' + index + ']'
	elif addr_mode == 'abs_x':
		return base + '[' + index + ' + reg_x]'
	elif addr_mode == 'abs_y':
			return base + '[' + index + ' + reg_y]'
	else:
		return 'mem_r(*(uint16_t*)&' + base + '[' + index + '] + reg_y)'

def write_c_sub(label_idx: int, s: io.TextIOBase):
	line_idx = labels[label_idx].line + 1
	
	s.write('void ' + labels[label_idx].name + '() {\n')
	
	while line_idx != len(lines):
		line = lines[line_idx]
		
		c = ''
		indent = True
		
		if line.text.endswith(':'):
			indent = False
			label_idx += 1
			label = labels[label_idx]
			if label.kind == 'local':
				# Local labels get translated into C labels
				c = label.name + ':;'
			else:
				# We reached the next subroutine. This subroutine falls through
				# into it, so we translate that the same way as an unconditional
				# jump (calling the next subroutine and then returning).
				if label.kind == 'sub':
					s.write('\t' + label.name + '(); return;\n')
				# Then we exit the translation loop
				break
		elif line.text.startswith('.byte $2c'):
			# This is a BIT instruction used for a BIT-skip. This has to be
			# manually fixed up after translation
			c = '// .byte $2c // BIT-SKIP'
		elif line.text.startswith('.'):
			# We reached non-code data, so exit the translation loop
			break
		elif line.text == 'jsr JumpEngine':
			# This is a jump table... Collect the possible target subroutines in
			# an array
			targets: list[str] = []
			while True:
				line_idx += 1
				line = lines[line_idx]
				if not line.text.startswith('.word'):
					break
				targets.append(line.text[6:])
			
			# Translate into a C jump table
			s.write('\tstatic void(*targets[])() = {\n\t\t' +
				',\n\t\t'.join(targets) + '\n\t};\n')
				
			# Generate an indirect tail call
			s.write('\ttargets[reg_a](); return;\n')
			
			c = ''
			
			line_idx -= 1
		elif line.text == "sta PPUCTRL":
			c = "env_ppuctrl_w(reg_a);"
		elif line.text == "stx PPUCTRL":
			c = "env_ppuctrl_w(reg_x);"
		elif line.text == "sty PPUCTRL":
			c = "env_ppuctrl_w(reg_y);"
		elif line.text == "sta PPUMASK":
			c = "env_ppumask_w(reg_a);"
		elif line.text == "stx PPUMASK":
			c = "env_ppumask_w(reg_x);"
		elif line.text == "sty PPUMASK":
			c = "env_ppumask_w(reg_y);"
		elif line.text == "lda PPUSTATUS":
			c = "set_a(env_ppustatus_r());"
		elif line.text == "ldx PPUSTATUS":
			c = "set_x(env_ppustatus_r());"
		elif line.text == "ldy PPUSTATUS":
			c = "set_y(env_ppustatus_r());"
		elif line.text == "sta OAMADDR":
			c = "env_oamaddr_w(reg_a);"
		elif line.text == "stx OAMADDR":
			c = "env_oamaddr_w(reg_x);"
		elif line.text == "sty OAMADDR":
			c = "env_oamaddr_w(reg_y);"
		elif line.text == "sta PPUSCROLL":
			c = "env_ppuscroll_w(reg_a);"
		elif line.text == "stx PPUSCROLL":
			c = "env_ppuscroll_w(reg_x);"
		elif line.text == "sty PPUSCROLL":
			c = "env_ppuscroll_w(reg_y);"
		elif line.text == "sta PPUADDR":
			c = "env_ppuaddr_w(reg_a);"
		elif line.text == "stx PPUADDR":
			c = "env_ppuaddr_w(reg_x);"
		elif line.text == "sty PPUADDR":
			c = "env_ppuaddr_w(reg_y);"
		elif line.text == "sta PPUDATA":
			c = "env_ppudata_w(reg_a);"
		elif line.text == "stx PPUDATA":
			c = "env_ppudata_w(reg_x);"
		elif line.text == "sty PPUDATA":
			c = "env_ppudata_w(reg_y);"
		elif line.text == "lda PPUDATA":
			c = "set_a(env_ppudata_r());"
		elif line.text == "ldx PPUDATA":
			c = "set_x(env_ppudata_r());"
		elif line.text == "ldy PPUDATA":
			c = "set_y(env_ppudata_r());"
		elif line.text == "sta SQ1_VOL":
			c = "env_sq1_vol_w(reg_a);"
		elif line.text == "stx SQ1_VOL":
			c = "env_sq1_vol_w(reg_x);"
		elif line.text == "sty SQ1_VOL":
			c = "env_sq1_vol_w(reg_y);"
		elif line.text == "sta SQ1_SWEEP":
			c = "env_sq1_sweep_w(reg_a);"
		elif line.text == "stx SQ1_SWEEP":
			c = "env_sq1_sweep_w(reg_x);"
		elif line.text == "sty SQ1_SWEEP":
			c = "env_sq1_sweep_w(reg_y);"
		elif line.text == "sta SQ1_LO":
			c = "env_sq1_lo_w(reg_a);"
		elif line.text == "stx SQ1_LO":
			c = "env_sq1_lo_w(reg_x);"
		elif line.text == "sty SQ1_LO":
			c = "env_sq1_lo_w(reg_y);"
		elif line.text == "sta SQ1_LO,x":
			c = "if (reg_x==0) { env_sq1_lo_w(reg_a); } else if (reg_x==4) { env_sq2_lo_w(reg_a); } else if (reg_x==8) { env_tri_lo_w(reg_a); }"
		elif line.text == "sta SQ1_HI,x":
			c = "if (reg_x==0) { env_sq1_hi_w(reg_a); } else if (reg_x==4) { env_sq2_hi_w(reg_a); } else if (reg_x==8) { env_tri_hi_w(reg_a); }"
		elif line.text == "sta SQ1_HI":
			c = "env_sq1_hi_w(reg_a);"
		elif line.text == "stx SQ1_HI":
			c = "env_sq1_hi_w(reg_x);"
		elif line.text == "sty SQ1_HI":
			c = "env_sq1_hi_w(reg_y);"
		elif line.text == "sta SQ2_VOL":
			c = "env_sq2_vol_w(reg_a);"
		elif line.text == "stx SQ2_VOL":
			c = "env_sq2_vol_w(reg_x);"
		elif line.text == "sty SQ2_VOL":
			c = "env_sq2_vol_w(reg_y);"
		elif line.text == "sta SQ2_SWEEP":
			c = "env_sq2_sweep_w(reg_a);"
		elif line.text == "stx SQ2_SWEEP":
			c = "env_sq2_sweep_w(reg_x);"
		elif line.text == "sty SQ2_SWEEP":
			c = "env_sq2_sweep_w(reg_y);"
		elif line.text == "sta SQ2_LO":
			c = "env_sq2_lo_w(reg_a);"
		elif line.text == "stx SQ2_LO":
			c = "env_sq2_lo_w(reg_x);"
		elif line.text == "sty SQ2_LO":
			c = "env_sq2_lo_w(reg_y);"
		elif line.text == "sta TRI_LINEAR":
			c = "env_tri_linear_w(reg_a);"
		elif line.text == "stx TRI_LINEAR":
			c = "env_tri_linear_w(reg_x);"
		elif line.text == "sty TRI_LINEAR":
			c = "env_tri_linear_w(reg_y);"
		elif line.text == "sta NOISE_VOL":
			c = "env_noise_vol_w(reg_a);"
		elif line.text == "stx NOISE_VOL":
			c = "env_noise_vol_w(reg_x);"
		elif line.text == "sty NOISE_VOL":
			c = "env_noise_vol_w(reg_y);"
		elif line.text == "sta NOISE_LO":
			c = "env_noise_lo_w(reg_a);"
		elif line.text == "stx NOISE_LO":
			c = "env_noise_lo_w(reg_x);"
		elif line.text == "sty NOISE_LO":
			c = "env_noise_lo_w(reg_y);"
		elif line.text == "sta NOISE_HI":
			c = "env_noise_hi_w(reg_a);"
		elif line.text == "stx NOISE_HI":
			c = "env_noise_hi_w(reg_x);"
		elif line.text == "sty NOISE_HI":
			c = "env_noise_hi_w(reg_y);"
		elif line.text == "sta DMC_RAW":
			c = "env_dmc_raw_w(reg_a);"
		elif line.text == "stx DMC_RAW":
			c = "env_dmc_raw_w(reg_x);"
		elif line.text == "sty DMC_RAW":
			c = "env_dmc_raw_w(reg_y);"
		elif line.text == "sta OAMDMA":
			c = "env_oamdma_w(reg_a);"
		elif line.text == "stx OAMDMA":
			c = "env_oamdma_w(reg_x);"
		elif line.text == "sty OAMDMA":
			c = "env_oamdma_w(reg_y);"
		elif line.text == "sta SND_CHN":
			c = "env_snd_chn_w(reg_a);"
		elif line.text == "stx SND_CHN":
			c = "env_snd_chn_w(reg_x);"
		elif line.text == "sty SND_CHN":
			c = "env_snd_chn_w(reg_y);"
		elif line.text == "sta JOY1":
			c = "env_joy1_w(reg_a);"
		elif line.text == "stx JOY1":
			c = "env_joy1_w(reg_x);"
		elif line.text == "sty JOY1":
			c = "env_joy1_w(reg_y);"
		elif line.text == "lda JOY1,x":
			c = "set_a((reg_x==0)? env_joy1_r() : env_joy2_r());"
		elif line.text == "sta JOY2":
			c = "env_joy2_w(reg_a);"
		elif line.text == "stx JOY2":
			c = "env_joy2_w(reg_x);"
		elif line.text == "sty JOY2":
			c = "env_joy2_w(reg_y);"
		else:
			opc = line.text[:3]
			
			if len(line.text) <= 4:
				match opc:
					case 'rts':
						c = 'return;'
					case 'rti':
						c = 'return;'
					case "clc":
						c = "reg_p.c = 0;"
					case "sec":
						c = "reg_p.c = 1;"
					case "cli":
						c = "reg_p.i = 0;"
					case "sei":
						c = "reg_p.i = 1;"
					case "clv":
						c = "reg_p.v = 0;"
					case "cld":
						c = "reg_p.d = 0;"
					case "sed":
						c = "reg_p.d = 1;"
					case "tax":
						c = "set_x(reg_a);"
					case "tay":
						c = "set_y(reg_a);"
					case "txa":
						c = "set_a(reg_x);"
					case "txs":
						c = "reg_s = reg_x;"
					case "tya":
						c = "set_a(reg_y);"
					case "inx":
						c = "set_x(reg_x+1);"
					case "iny":
						c = "set_y(reg_y+1);"
					case "dex":
						c = "set_x(reg_x-1);"
					case "dey":
						c = "set_y(reg_y-1);"
					case "asl":
						c = "reg_a = shl(reg_a);"
					case "lsr":
						c = "reg_a = shr(reg_a);"
					case "rol":
						c = "reg_a = rol(reg_a);"
					case "ror":
						c = "reg_a = ror(reg_a);"
					case "pha":
						c = "push(reg_a);"
					case "pla":
						c = "set_a(pull());"
			else:
				oa = line.text[4:]
				
				if opc in ['bpl', 'bmi', 'bvc', 'bvs', 'bcc', 'bcs', 'bne', 'beq', 'jmp', 'jsr']:
					addr_mode = 'code'
					o_c = oa
				elif oa.startswith('#'):
					addr_mode = 'imm'
					o_c = oa[1:]
				elif oa.endswith(',x'):
					addr_mode = 'abs_x'
					o_c = oa[:-2]
				elif oa.endswith('),y'):
					addr_mode = 'ind_y'
					o_c = oa[1:-3]
				elif oa.endswith(',y'):
					addr_mode = 'abs_y'
					o_c = oa[:-2]
				else:
					addr_mode = 'abs'
					o_c = oa
				
				o_c = operand_as_c(o_c, addr_mode)
				
				jmp_to_oc = ''
				if addr_mode == 'code':
					if o_c in sub_label_names:
						jmp_to_oc = f'{o_c}(); return;'
					else:
						jmp_to_oc = f'goto {o_c};'
				
				match opc:
					case 'adc':
						c = f'add_a({o_c});'
					case 'and':
						c = f'and_a({o_c});'
					case 'asl':
						c = f'{o_c} = shl({o_c});'
					case 'bcc':
						c = f'if (!reg_p.c) {{ {jmp_to_oc} }}'
					case 'bcs':
						c = f'if (reg_p.c) {{ {jmp_to_oc} }}'
					case 'beq':
						c = f'if (reg_p.z) {{ {jmp_to_oc} }}'
					case 'bit':
						c = f'bit_a({o_c});'
					case "bmi":
						c = f"if (reg_p.n) {{ {jmp_to_oc} }}"
					case "bne":
						c = f"if (!reg_p.z) {{ {jmp_to_oc} }}"
					case "bpl":
						c = f"if (!reg_p.n) {{ {jmp_to_oc} }}"
					case "bvc":
						c = f"if (!reg_p.v) {{ {jmp_to_oc} }}"
					case "bvs":
						c = f"if (reg_p.v) {{ {jmp_to_oc} }}"
					case "cmp":
						c = f"cmp_a({o_c});"
					case "cpx":
						c = f"cmp_x({o_c});"
					case "cpy":
						c = f"cmp_y({o_c});"
					case "dec":
						c = f"{o_c} = dec({o_c});"
					case "eor":
						c = f"eor_a({o_c});"
					case "inc":
						c = f"{o_c} = inc({o_c});"
					case "jmp":
						c = f"{jmp_to_oc}"
					case "jsr":
						c = f"{o_c}();"
					case "lda":
						c = f"set_a({o_c});"
					case "ldx":
						c = f"set_x({o_c});"
					case "ldy":
						c = f"set_y({o_c});"
					case "lsr":
						c = f"{o_c} = shr({o_c});"
					case "nop":
						c = ""
					case "ora":
						c = f"or_a({o_c});"
					case "rol":
						c = f"{o_c} = rol({o_c});"
					case "ror":
						c = f"{o_c} = ror({o_c});"
					case "sbc":
						c = f"sub_a({o_c});"
					case 'sta':
						c = f'{o_c} = reg_a;'
						if addr_mode == 'ind_y':
							c = f'mem_w{o_c[5:-1]}, reg_a);'
					case 'stx':
						c = f'{o_c} = reg_x;'
						if addr_mode == 'ind_y':
							c = f'mem_w{o_c[5:-1]}, reg_x);'
					case 'sty':
						c = f'{o_c} = reg_y;'
						if addr_mode == 'ind_y':
							c = f'mem_w{o_c[5:-1]}, reg_y);'
		
		if c != '':
			if indent:
				s.write(f'\t{c}')
			else:
				s.write(c)
			
			if line.post_comment != '':
				s.write(f' // {line.post_comment}')
			
			s.write('\n')
		
		line_idx += 1
	
	s.write('}\n\n')

def write_c():
	with open('smb.h', 'w') as s:
		s.write('#include "compat.h"\n#include "env.h"\n\n')
		
		for label in labels:
			if label.kind == 'sub':
				s.write('void ' + label.name + '();\n')
		s.write('\n')
	
	with open('smb.c', 'w') as s:
		s.write('#include "smb.h"\n\n')
		
		for i, label in enumerate(labels):
			if label.kind == 'sub' and label.name != 'JumpEngine':
				write_c_sub(i, s)

def translate():
	with open('smb.s', 'r', encoding='utf-8') as s:
		text_lines = s.readlines()
	
	print('parsing lines...')
	parse_lines(text_lines)
	print('parsed', len(lines), 'lines')
	
	print('finding labels...')
	find_labels()
	print('found', len(labels), 'labels')
	print('\tof which', sum(label.kind != 'data' for label in labels), 'were code labels')
	print('\tand', sum(label.kind == 'data' for label in labels), 'were data labels')
	
	print('finding subroutines...')
	find_subs()
	print('found', sum(label.kind == 'sub' for label in labels), 'subroutines')
	
	keep_searching = True
	while keep_searching:
		print('finding more subroutines...')
		keep_searching = find_more_subs()
		print('found', sum(label.kind == 'sub' for label in labels), 'subroutines')
	
	global data_label_names
	data_label_names = [label.name for label in labels if label.kind == 'data']
	
	global sub_label_names
	sub_label_names = [label.name for label in labels if label.kind == 'sub']
	
	print('writing c code...')
	write_c()

translate()
