/*============================================================================
  WCSLIB 8.4 - an implementation of the FITS WCS standard.
  Copyright (C) 1995-2024, Mark Calabretta

  This file is part of WCSLIB.

  WCSLIB is free software: you can redistribute it and/or modify it under the
  terms of the GNU Lesser General Public License as published by the Free
  Software Foundation, either version 3 of the License, or (at your option)
  any later version.

  WCSLIB is distributed in the hope that it will be useful, but WITHOUT ANY
  WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
  FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for
  more details.

  You should have received a copy of the GNU Lesser General Public License
  along with WCSLIB.  If not, see http://www.gnu.org/licenses.

  Author: Mark Calabretta, Australia Telescope National Facility, CSIRO.
  http://www.atnf.csiro.au/people/Mark.Calabretta
  $Id: wcsulex.l,v 8.4 2024/10/28 13:56:16 mcalabre Exp $
*=============================================================================
*
* wcsulex.l is a Flex description file containing the definition of a
* recursive, multi-buffered lexical scanner and parser for FITS units
* specifications.
*
* It requires Flex v2.5.4 or later.
*
* Refer to wcsunits.h for a description of the user interface and operating
* notes.
*
*===========================================================================*/

/* Options. */
%option full
%option never-interactive
%option noinput
%option noyywrap
%option outfile="wcsulex.c"
%option prefix="wcsulex"
%option reentrant
%option extra-type="struct wcsulex_extra *"

/* Exponents. */
INTEGER	  [+-]?[1-9][0-9]*
FRAC	  {INTEGER}"/"[1-9][0-9]*
FLOAT	  [+-]?([0-9]+\.?[0-9]*|\.[0-9]+)

/* Metric prefixes. */
SUB3	  [munpfazy]
SUBPREFIX [dc]|{SUB3}
SUP3	  [kMGTPEZY]
SUPPREFIX da|h|{SUP3}
PREFIX	  {SUBPREFIX}|{SUPPREFIX}

/* Basic and derived SI units. */
BASIC	  m|s|g|rad|sr|K|A|mol|cd
DERIVED	  Hz|J|W|V|N|Pa|C|[Oo]hm|S|F|Wb|T|H|lm|lx
SI_UNIT	  {BASIC}|{DERIVED}

/* Additional recognized units: all metric prefixes allowed. */
ADD_ALL	  eV|Jy|R|G|barn

/* Additional recognized units: only super-metric prefixes allowed. */
ADD_SUP	  a|yr|pc|bit|[bB]yte

/* Additional recognized units: only sub-metric prefixes allowed. */
ADD_SUB	  mag

/* Additional recognized units for which NO metric prefixes are allowed. */
GENERAL	  deg|arcmin|arcsec|mas|turn|min|h|d|cy|erg|Ry|u|D
ASTRO	  [Aa]ngstrom|AU|lyr|beam|solRad|solMass|solLum|Sun
DEVICE	  adu|bin|chan|count|ct|photon|ph|pixel|pix|voxel
ADD_NONE  {GENERAL}|{ASTRO}|{DEVICE}

/* All additional recognized units. */
ADD_UNIT  {ADD_ALL}|{ADD_SUP}|{ADD_SUB}|{ADD_NONE}

/* Exclusive start states. */
%x PAREN PREFIX UNITS EXPON FLUSH

%{
#include <math.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>

#include "wcserr.h"
#include "wcsmath.h"
#include "wcsunits.h"
#include "wcsutil.h"

// User data associated with yyscanner.
struct wcsulex_extra {
  // Used in preempting the call to exit() by yy_fatal_error().
  jmp_buf abort_jmp_env;
};

#define YY_DECL int wcsulexe_scanner(const char unitstr[], int *func, \
 double *scale, double units[WCSUNITS_NTYPE], struct wcserr **err, \
 yyscan_t yyscanner)

// Dummy definition to circumvent compiler warnings.
#define YY_INPUT(inbuff, count, bufsize) { count = YY_NULL; }

// Preempt the call to exit() by yy_fatal_error().
#define exit(status) longjmp(yyextra->abort_jmp_env, status);

// Internal helper functions.
static YY_DECL;

%}

%%
	static const char *function = "wcsulexe_scanner";
	
	void add(double *factor, double types[], double *expon, double *scale,
	    double units[]);
	
	// Initialise returned values.
	*func  = 0;
	*scale = 1.0;
	
	for (int i = 0; i < WCSUNITS_NTYPE; i++) {
	  units[i] = 0.0;
	}
	
	if (err) *err = 0x0;
	
	double types[WCSUNITS_NTYPE];
	for (int i = 0; i < WCSUNITS_NTYPE; i++) {
	  types[i] = 0.0;
	}
	double expon  = 1.0;
	double factor = 1.0;
	
	int bracket  = 0;
	int operator = 0;
	int paren    = 0;
	int status   = 0;
	
	// Avert a flex-induced memory leak.
	if (YY_CURRENT_BUFFER && YY_CURRENT_BUFFER->yy_input_file == stdin) {
	  yy_delete_buffer(YY_CURRENT_BUFFER, yyscanner);
	}
	
	yy_scan_string(unitstr, yyscanner);
	
	// Return here via longjmp() invoked by yy_fatal_error().
	if (setjmp(yyextra->abort_jmp_env)) {
	  return wcserr_set(WCSERR_SET(UNITSERR_PARSER_ERROR),
	    "Internal units parser error parsing '%s'", unitstr);
	}
	
	BEGIN(INITIAL);
	
	#ifdef DEBUG
	fprintf(stderr, "\n%s ->\n", unitstr);
	#endif

^" "+	{
	  // Pretend initial whitespace doesn't exist.
	  yy_set_bol(1);
	}

^"["	{
	  if (bracket++) {
	    BEGIN(FLUSH);
	  } else {
	    yy_set_bol(1);
	  }
	}

^10[0-9] {
	  status = wcserr_set(WCSERR_SET(UNITSERR_BAD_NUM_MULTIPLIER),
	    "Invalid exponent in '%s'", unitstr);
	  BEGIN(FLUSH);
	}

^10	{
	  factor = 10.0;
	  BEGIN(EXPON);
	}

^log" "*"(" {
	  *func = 1;
	  unput('(');
	  BEGIN(PAREN);
	}

^ln" "*"(" {
	  *func = 2;
	  unput('(');
	  BEGIN(PAREN);
	}

^exp" "*"(" {
	  *func = 3;
	  unput('(');
	  BEGIN(PAREN);
	}

^[*.]	{
	  // Leading binary multiply.
	  status = wcserr_set(WCSERR_SET(UNITSERR_DANGLING_BINOP),
	    "Dangling binary operator in '%s'", unitstr);
	  BEGIN(FLUSH);
	}

" "+	  // Discard whitespace in INITIAL context.

sqrt" "*"(" {
	  expon /= 2.0;
	  unput('(');
	  BEGIN(PAREN);
	}

"("	{
	  // Gather terms in parentheses.
	  yyless(0);
	  BEGIN(PAREN);
	}

[*.]	{
	  if (operator++) {
	    BEGIN(FLUSH);
	  }
	}

^1"/" |
"/"	{
	  if (operator++) {
	    BEGIN(FLUSH);
	  } else {
	    expon *= -1.0;
	  }
	}

{SI_UNIT}|{ADD_UNIT} {
	  operator = 0;
	  yyless(0);
	  BEGIN(UNITS);
	}

{PREFIX}({SI_UNIT}|{ADD_ALL}) |
{SUPPREFIX}{ADD_SUP} |
{SUBPREFIX}{ADD_SUB} {
	  operator = 0;
	  yyless(0);
	  BEGIN(PREFIX);
	}

"]"	{
	  bracket = !bracket;
	  BEGIN(FLUSH);
	}

.	{
	  status = wcserr_set(WCSERR_SET(UNITSERR_BAD_INITIAL_SYMBOL),
	    "Invalid symbol in INITIAL context in '%s'", unitstr);
	  BEGIN(FLUSH);
	}

<PAREN>"(" {
	  paren++;
	  operator = 0;
	  yymore();
	}

<PAREN>")" {
	  paren--;
	  if (paren) {
	    // Not balanced yet.
	    yymore();
	
	  } else {
	    // Balanced; strip off the outer parentheses and recurse.
	    yytext[yyleng-1] = '\0';
	
	    int func_r;
	    double factor_r;
	    status = wcsulexe(yytext+1, &func_r, &factor_r, types, err);
	
	    YY_BUFFER_STATE buf = YY_CURRENT_BUFFER;
	    yy_switch_to_buffer(buf, yyscanner);
	
	    if (func_r) {
	      status = wcserr_set(WCSERR_SET(UNITSERR_FUNCTION_CONTEXT),
	        "Function in invalid context in '%s'", unitstr);
	    }
	
	    if (status) {
	      BEGIN(FLUSH);
	    } else {
	      factor *= factor_r;
	      BEGIN(EXPON);
	    }
	  }
	}

<PAREN>[^()]+ {
	  yymore();
	}

<PREFIX>d {
	  factor = 1e-1;
	  BEGIN(UNITS);
	}

<PREFIX>c {
	  factor = 1e-2;
	  BEGIN(UNITS);
	}

<PREFIX>m {
	  factor = 1e-3;
	  BEGIN(UNITS);
	}

<PREFIX>u {
	  factor = 1e-6;
	  BEGIN(UNITS);
	}

<PREFIX>n {
	  factor = 1e-9;
	  BEGIN(UNITS);
	}

<PREFIX>p {
	  factor = 1e-12;
	  BEGIN(UNITS);
	}

<PREFIX>f {
	  factor = 1e-15;
	  BEGIN(UNITS);
	}

<PREFIX>a {
	  factor = 1e-18;
	  BEGIN(UNITS);
	}

<PREFIX>z {
	  factor = 1e-21;
	  BEGIN(UNITS);
	}

<PREFIX>y {
	  factor = 1e-24;
	  BEGIN(UNITS);
	}

<PREFIX>da {
	  factor = 1e+1;
	  BEGIN(UNITS);
	}

<PREFIX>h {
	  factor = 1e+2;
	  BEGIN(UNITS);
	}

<PREFIX>k {
	  factor = 1e+3;
	  BEGIN(UNITS);
	}

<PREFIX>M {
	  factor = 1e+6;
	  BEGIN(UNITS);
	}

<PREFIX>G {
	  factor = 1e+9;
	  BEGIN(UNITS);
	}

<PREFIX>T {
	  factor = 1e+12;
	  BEGIN(UNITS);
	}

<PREFIX>P {
	  factor = 1e+15;
	  BEGIN(UNITS);
	}

<PREFIX>E {
	  factor = 1e+18;
	  BEGIN(UNITS);
	}

<PREFIX>Z {
	  factor = 1e+21;
	  BEGIN(UNITS);
	}

<PREFIX>Y {
	  factor = 1e+24;
	  BEGIN(UNITS);
	}

<PREFIX>. {
	  // Internal parser error.
	  status = wcserr_set(WCSERR_SET(UNITSERR_PARSER_ERROR),
	    "Internal units parser error parsing '%s'", unitstr);
	  BEGIN(FLUSH);
	}

<UNITS>A {
	  // Ampere.
	  types[WCSUNITS_CHARGE] += 1.0;
	  types[WCSUNITS_TIME]   -= 1.0;
	  BEGIN(EXPON);
	}

<UNITS>a|yr {
	  // Julian year (annum).
	  factor *= 31557600.0;
	  types[WCSUNITS_TIME] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>adu {
	  // Analogue-to-digital converter units.
	  types[WCSUNITS_COUNT] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>[Aa]ngstrom {
	  // Angstrom.
	  factor *= 1e-10;
	  types[WCSUNITS_LENGTH] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>arcmin {
	  // Minute of arc.
	  factor /= 60.0;
	  types[WCSUNITS_PLANE_ANGLE] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>arcsec {
	  // Second of arc.
	  factor /= 3600.0;
	  types[WCSUNITS_PLANE_ANGLE] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>AU {
	  // Astronomical unit.
	  factor *= 1.49598e+11;
	  types[WCSUNITS_LENGTH] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>barn {
	  // Barn.
	  factor *= 1e-28;
	  types[WCSUNITS_LENGTH] += 2.0;
	  BEGIN(EXPON);
	}

<UNITS>beam {
	  // Beam, as in Jy/beam.
	  types[WCSUNITS_BEAM] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>bin {
	  // Bin (e.g. histogram).
	  types[WCSUNITS_BIN] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>bit {
	  // Bit.
	  types[WCSUNITS_BIT] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>[bB]yte {
	  // Byte.
	  factor *= 8.0;
	  types[WCSUNITS_BIT] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>C {
	  // Coulomb.
	  types[WCSUNITS_CHARGE] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>cd {
	  // Candela.
	  types[WCSUNITS_LUMINTEN] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>chan {
	  // Channel.
	  types[WCSUNITS_BIN] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>count|ct {
	  // Count.
	  types[WCSUNITS_COUNT] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>cy {
	  // Julian century.
	  factor *= 3155760000.0;
	  types[WCSUNITS_TIME] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>D {
	  // Debye.
	  factor *= 1e-29 / 3.0;
	  types[WCSUNITS_CHARGE] += 1.0;
	  types[WCSUNITS_LENGTH] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>d {
	  // Day.
	  factor *= 86400.0;
	  types[WCSUNITS_TIME] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>deg {
	  // Degree.
	  types[WCSUNITS_PLANE_ANGLE] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>erg {
	  // Erg.
	  factor *= 1e-7;
	  types[WCSUNITS_MASS]   += 1.0;
	  types[WCSUNITS_LENGTH] += 2.0;
	  types[WCSUNITS_TIME]   -= 2.0;
	  BEGIN(EXPON);
	}

<UNITS>eV {
	  // Electron volt.
	  factor *= 1.6021765e-19;
	  types[WCSUNITS_MASS]   += 1.0;
	  types[WCSUNITS_LENGTH] += 2.0;
	  types[WCSUNITS_TIME]   -= 2.0;
	  BEGIN(EXPON);
	}

<UNITS>F {
	  // Farad.
	  types[WCSUNITS_MASS]   -= 1.0;
	  types[WCSUNITS_LENGTH] -= 2.0;
	  types[WCSUNITS_TIME]   += 3.0;
	  types[WCSUNITS_CHARGE] += 2.0;
	  BEGIN(EXPON);
	}

<UNITS>G {
	  // Gauss.
	  factor *= 1e-4;
	  types[WCSUNITS_MASS]   += 1.0;
	  types[WCSUNITS_TIME]   += 1.0;
	  types[WCSUNITS_CHARGE] -= 1.0;
	  BEGIN(EXPON);
	}

<UNITS>g {
	  // Gram.
	  factor *= 1e-3;
	  types[WCSUNITS_MASS] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>H {
	  // Henry.
	  types[WCSUNITS_MASS]   += 1.0;
	  types[WCSUNITS_LENGTH] += 2.0;
	  types[WCSUNITS_TIME]   += 2.0;
	  types[WCSUNITS_CHARGE] -= 2.0;
	  BEGIN(EXPON);
	}

<UNITS>h {
	  // Hour.
	  factor *= 3600.0;
	  types[WCSUNITS_TIME] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>Hz {
	  // Hertz.
	  types[WCSUNITS_TIME] -= 1.0;
	  BEGIN(EXPON);
	}

<UNITS>J {
	  // Joule.
	  types[WCSUNITS_MASS]   += 1.0;
	  types[WCSUNITS_LENGTH] += 2.0;
	  types[WCSUNITS_TIME]   -= 2.0;
	  BEGIN(EXPON);
	}

<UNITS>Jy {
	  // Jansky.
	  factor *= 1e-26;
	  types[WCSUNITS_MASS] += 1.0;
	  types[WCSUNITS_TIME] -= 2.0;
	  BEGIN(EXPON);
	}

<UNITS>K {
	  // Kelvin.
	  types[WCSUNITS_TEMPERATURE] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>lm {
	  // Lumen.
	  types[WCSUNITS_LUMINTEN]    += 1.0;
	  types[WCSUNITS_SOLID_ANGLE] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>lx {
	  // Lux.
	  types[WCSUNITS_LUMINTEN]    += 1.0;
	  types[WCSUNITS_SOLID_ANGLE] += 1.0;
	  types[WCSUNITS_LENGTH]      -= 2.0;
	  BEGIN(EXPON);
	}

<UNITS>lyr {
	  // Light year.
	  factor *= 2.99792458e8 * 31557600.0;
	  types[WCSUNITS_LENGTH] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>m {
	  // Metre.
	  types[WCSUNITS_LENGTH] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>mag {
	  // Stellar magnitude.
	  types[WCSUNITS_MAGNITUDE] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>mas {
	  // Milli-arcsec.
	  factor /= 3600e+3;
	  types[WCSUNITS_PLANE_ANGLE] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>min {
	  // Minute.
	  factor *= 60.0;
	  types[WCSUNITS_TIME] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>mol {
	  // Mole.
	  types[WCSUNITS_MOLE] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>N {
	  // Newton.
	  types[WCSUNITS_MASS]   += 1.0;
	  types[WCSUNITS_LENGTH] += 1.0;
	  types[WCSUNITS_TIME]   -= 2.0;
	  BEGIN(EXPON);
	}

<UNITS>[Oo]hm {
	  // Ohm.
	  types[WCSUNITS_MASS]   += 1.0;
	  types[WCSUNITS_LENGTH] += 2.0;
	  types[WCSUNITS_TIME]   -= 1.0;
	  types[WCSUNITS_CHARGE] -= 2.0;
	  BEGIN(EXPON);
	}

<UNITS>Pa {
	  // Pascal.
	  types[WCSUNITS_MASS]   += 1.0;
	  types[WCSUNITS_LENGTH] -= 1.0;
	  types[WCSUNITS_TIME]   -= 2.0;
	  BEGIN(EXPON);
	}

<UNITS>pc {
	  // Parsec.
	  factor *= 3.0857e16;
	  types[WCSUNITS_LENGTH] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>photon|ph {
	  // Photon.
	  types[WCSUNITS_COUNT] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>pixel|pix {
	  // Pixel.
	  types[WCSUNITS_PIXEL] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>R {
	  // Rayleigh.
	  factor *= 1e10 / (4.0 * PI);
	  types[WCSUNITS_LENGTH]      -= 2.0;
	  types[WCSUNITS_TIME]        -= 1.0;
	  types[WCSUNITS_SOLID_ANGLE] -= 1.0;
	  BEGIN(EXPON);
	}

<UNITS>rad {
	  // Radian.
	  factor *= 180.0 / PI;
	  types[WCSUNITS_PLANE_ANGLE] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>Ry {
	  // Rydberg.
	  factor *= 13.605692 * 1.6021765e-19;
	  types[WCSUNITS_MASS]   += 1.0;
	  types[WCSUNITS_LENGTH] += 2.0;
	  types[WCSUNITS_TIME]   -= 2.0;
	  BEGIN(EXPON);
	}

<UNITS>S {
	  // Siemen.
	  types[WCSUNITS_MASS]   -= 1.0;
	  types[WCSUNITS_LENGTH] -= 2.0;
	  types[WCSUNITS_TIME]   += 1.0;
	  types[WCSUNITS_CHARGE] += 2.0;
	  BEGIN(EXPON);
	}

<UNITS>s {
	  // Second.
	  types[WCSUNITS_TIME] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>solLum {
	  // Solar luminosity.
	  factor *= 3.8268e26;
	  types[WCSUNITS_MASS]   += 1.0;
	  types[WCSUNITS_LENGTH] += 2.0;
	  types[WCSUNITS_TIME]   -= 3.0;
	  BEGIN(EXPON);
	}

<UNITS>solMass {
	  // Solar mass.
	  factor *= 1.9891e30;
	  types[WCSUNITS_MASS] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>solRad {
	  // Solar radius.
	  factor *= 6.9599e8;
	  types[WCSUNITS_LENGTH] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>sr {
	  // Steradian.
	  types[WCSUNITS_SOLID_ANGLE] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>Sun {
	  // Sun (with respect to).
	  types[WCSUNITS_SOLRATIO] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>T {
	  // Tesla.
	  types[WCSUNITS_MASS]   += 1.0;
	  types[WCSUNITS_TIME]   += 1.0;
	  types[WCSUNITS_CHARGE] -= 1.0;
	  BEGIN(EXPON);
	}

<UNITS>turn {
	  // Turn.
	  factor *= 360.0;
	  types[WCSUNITS_PLANE_ANGLE] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>u {
	  // Unified atomic mass unit.
	  factor *= 1.6605387e-27;
	  types[WCSUNITS_MASS] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>V {
	  // Volt.
	  types[WCSUNITS_MASS]   += 1.0;
	  types[WCSUNITS_LENGTH] += 1.0;
	  types[WCSUNITS_TIME]   -= 2.0;
	  types[WCSUNITS_CHARGE] -= 1.0;
	  BEGIN(EXPON);
	}

<UNITS>voxel {
	  // Voxel.
	  types[WCSUNITS_VOXEL] += 1.0;
	  BEGIN(EXPON);
	}

<UNITS>W {
	  // Watt.
	  types[WCSUNITS_MASS]   += 1.0;
	  types[WCSUNITS_LENGTH] += 2.0;
	  types[WCSUNITS_TIME]   -= 3.0;
	  BEGIN(EXPON);
	}

<UNITS>Wb {
	  // Weber.
	  types[WCSUNITS_MASS]   += 1.0;
	  types[WCSUNITS_LENGTH] += 2.0;
	  types[WCSUNITS_TIME]   += 1.0;
	  types[WCSUNITS_CHARGE] -= 1.0;
	  BEGIN(EXPON);
	}

<UNITS>. {
	  // Internal parser error.
	  status = wcserr_set(WCSERR_SET(UNITSERR_PARSER_ERROR),
	    "Internal units parser error parsing '%s'", unitstr);
	  BEGIN(FLUSH);
	}

<EXPON>" "*("**"|^) {
	  // Exponentiation.
	  if (operator++) {
	    BEGIN(FLUSH);
	  }
	}

<EXPON>" "*{INTEGER} {
	  int i;
	  sscanf(yytext, " %d", &i);
	  expon *= (double)i;
	  add(&factor, types, &expon, scale, units);
	  operator = 0;
	  BEGIN(INITIAL);
	}

<EXPON>" "*"("" "*{INTEGER}" "*")" {
	  int i;
	  sscanf(yytext, " (%d)", &i);
	  expon *= (double)i;
	  add(&factor, types, &expon, scale, units);
	  operator = 0;
	  BEGIN(INITIAL);
	}

<EXPON>" "*"("" "*{FRAC}" "*")" {
	  int i, j;
	  sscanf(yytext, " (%d/%d)", &i, &j);
	  expon *= (double)i / (double)j;
	  add(&factor, types, &expon, scale, units);
	  operator = 0;
	  BEGIN(INITIAL);
	}

<EXPON>" "*"("" "*{FLOAT}" "*")" {
	  char ctmp[72];
	  sscanf(yytext, " (%s)", ctmp);
	  double dexp;
	  wcsutil_str2double(ctmp, &dexp);
	  expon *= dexp;
	  add(&factor, types, &expon, scale, units);
	  operator = 0;
	  BEGIN(INITIAL);
	}

<EXPON>" "*[.*]" "* {
	  // Multiply.
	  if (operator++) {
	    BEGIN(FLUSH);
	  } else {
	    add(&factor, types, &expon, scale, units);
	    BEGIN(INITIAL);
	  }
	}

<EXPON>" "*"(" {
	  // Multiply.
	  if (operator) {
	    BEGIN(FLUSH);
	  } else {
	    add(&factor, types, &expon, scale, units);
	    unput('(');
	    BEGIN(INITIAL);
	  }
	}

<EXPON>" "+ {
	  // Multiply.
	  if (operator) {
	    BEGIN(FLUSH);
	  } else {
	    add(&factor, types, &expon, scale, units);
	    BEGIN(INITIAL);
	  }
	}

<EXPON>" "*"/"" "* {
	  // Divide.
	  if (operator++) {
	    BEGIN(FLUSH);
	  } else {
	    add(&factor, types, &expon, scale, units);
	    expon = -1.0;
	    BEGIN(INITIAL);
	  }
	}

<EXPON>" "*"]" {
	  add(&factor, types, &expon, scale, units);
	  bracket = !bracket;
	  BEGIN(FLUSH);
	}

<EXPON>. {
	  status = wcserr_set(WCSERR_SET(UNITSERR_BAD_EXPON_SYMBOL),
	    "Invalid symbol in EXPON context in '%s'", unitstr);
	  BEGIN(FLUSH);
	}

<FLUSH>.* {
	  // Discard any remaining input.
	}

<<EOF>>	{
	  // End-of-string.
	  if (YY_START == EXPON) {
	    add(&factor, types, &expon, scale, units);
	  }
	
	  if (bracket) {
	    status = wcserr_set(WCSERR_SET(UNITSERR_UNBAL_BRACKET),
	      "Unbalanced bracket in '%s'", unitstr);
	  } else if (paren) {
	    status = wcserr_set(WCSERR_SET(UNITSERR_UNBAL_PAREN),
	      "Unbalanced parenthesis in '%s'", unitstr);
	  } else if (operator == 1) {
	    status = wcserr_set(WCSERR_SET(UNITSERR_DANGLING_BINOP),
	      "Dangling binary operator in '%s'", unitstr);
	  } else if (operator) {
	    status = wcserr_set(WCSERR_SET(UNITSERR_CONSEC_BINOPS),
	      "Consecutive binary operators in '%s'", unitstr);
	  #ifdef DEBUG
	  } else {
	    fprintf(stderr, "EOS\n");
	  #endif
	  }
	
	  if (status) {
	    for (int i = 0; i < WCSUNITS_NTYPE; i++) {
	      units[i] = 0.0;
	      *scale = 0.0;
	    }
	  }
	
	  return status;
	}

%%

/*----------------------------------------------------------------------------
* External interface to the scanner.
*---------------------------------------------------------------------------*/

int wcsulexe(
  const char unitstr[],
  int *func,
  double *scale,
  double units[WCSUNITS_NTYPE],
  struct wcserr **err)

{
  // Function prototypes.
  int yylex_init_extra(YY_EXTRA_TYPE extra, yyscan_t *yyscanner);
  int yylex_destroy(yyscan_t yyscanner);

  struct wcsulex_extra extra;
  yyscan_t yyscanner;
  yylex_init_extra(&extra, &yyscanner);
  int status = wcsulexe_scanner(unitstr, func, scale, units, err, yyscanner);
  yylex_destroy(yyscanner);

  return status;
}


/*----------------------------------------------------------------------------
* Accumulate a term in a units specification and reset work variables.
*---------------------------------------------------------------------------*/

void add(
  double *factor,
  double types[],
  double *expon,
  double *scale,
  double units[])

{
  *scale *= pow(*factor, *expon);

  for (int i = 0; i < WCSUNITS_NTYPE; i++) {
    units[i] += *expon * types[i];
    types[i] = 0.0;
  }

  *expon  = 1.0;
  *factor = 1.0;

  return;
}
