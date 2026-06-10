//=============================================================================
// Copyright © 2025 NaturalPoint, Inc. All Rights Reserved.
// 
// THIS SOFTWARE IS GOVERNED BY THE OPTITRACK PLUGINS EULA AVAILABLE AT https://www.optitrack.com/about/legal/eula.html 
// AND/OR FOR DOWNLOAD WITH THE APPLICABLE SOFTWARE FILE(S) (“PLUGINS EULA”). BY DOWNLOADING, INSTALLING, ACTIVATING 
// AND/OR OTHERWISE USING THE SOFTWARE, YOU ARE AGREEING THAT YOU HAVE READ, AND THAT YOU AGREE TO COMPLY WITH AND ARE
// BOUND BY, THE PLUGINS EULA AND ALL APPLICABLE LAWS AND REGULATIONS. IF YOU DO NOT AGREE TO BE BOUND BY THE PLUGINS
// EULA, THEN YOU MAY NOT DOWNLOAD, INSTALL, ACTIVATE OR OTHERWISE USE THE SOFTWARE AND YOU MUST PROMPTLY DELETE OR
// RETURN IT. IF YOU ARE DOWNLOADING, INSTALLING, ACTIVATING AND/OR OTHERWISE USING THE SOFTWARE ON BEHALF OF AN ENTITY,
// THEN BY DOING SO YOU REPRESENT AND WARRANT THAT YOU HAVE THE APPROPRIATE AUTHORITY TO ACCEPT THE PLUGINS EULA ON
// BEHALF OF SUCH ENTITY. See license file in root directory for additional governing terms and information.
//=============================================================================

/*********************************************************************
 * \page   GLPrint.cpp
 * \file   GLPrint.cpp
 * \brief  
 *********************************************************************/

#include <stdio.h>
#include <stdarg.h>

#include <windows.h>
#include <gl/GL.h>
#include <gl/GLU.h>

#include "GLPrint.h"

/**
 * Set the device context.
 * 
 * \param hDC
 */
void GLPrint::SetDeviceContext(HDC hDC)
{
    HFONT	glFont;										
    HFONT	oldfont;
    LOGFONT logicalFont;

    // Display list for the 96 ascii characters (95 printable plus del)
    int numAsciiChars = 96;
    m_base = glGenLists(numAsciiChars);

    logicalFont.lfHeight          = -16;     
    logicalFont.lfWidth           = 0; 
    logicalFont.lfEscapement      = 0; 
    logicalFont.lfOrientation     = 0; 
    logicalFont.lfWeight          = FW_BLACK; //FW_NORMAL; 
    logicalFont.lfItalic          = FALSE; 
    logicalFont.lfUnderline       = FALSE; 
    logicalFont.lfStrikeOut       = FALSE; 
    logicalFont.lfCharSet         = ANSI_CHARSET; 
    logicalFont.lfOutPrecision    = OUT_TT_PRECIS; 
    logicalFont.lfClipPrecision   = CLIP_DEFAULT_PRECIS; 
    logicalFont.lfQuality         = ANTIALIASED_QUALITY; 
    logicalFont.lfPitchAndFamily  = FF_DONTCARE | DEFAULT_PITCH; 
    lstrcpy(logicalFont.lfFaceName, TEXT("Arial"));

    glFont = CreateFontIndirect( &logicalFont );
    oldfont = (HFONT)SelectObject(hDC, glFont);           

    /*
    wgl bitmap fonts
    - pre-rendered into display list
    - no rotation
    - scale independent
    */
    BOOL bSuccess = wglUseFontBitmaps(hDC, 32, numAsciiChars, m_base);

    // we're done with the font object so release it
    SelectObject(hDC, oldfont);
    DeleteObject(glFont);
}

/**
 * Formatted printing at (x,y).
 * 
 * \param x
 * \param y
 * \param format
 * \param 
 */
void GLPrint::Print(double x, double y, const char *format, ...)
{
  char		text[256];								
  va_list		ap;										
  if (format == nullptr)									
    return;											

  // parse formatted string/args into text string
  va_start(ap, format);									
  vsprintf_s(text, format, ap);					
  va_end(ap);											

  // wgl text
  glPushMatrix();
  glTranslated(x,y,0.0f);
  glRasterPos2d(0.0,0.0);
  // draw the text
  glListBase(m_base - 32);								
  glCallLists((GLsizei)strlen(text), GL_UNSIGNED_BYTE, text);	
  glPopMatrix();
}

